# api/tickets.py — Vercel Serverless Function (Python)
# Proxies Jira API to avoid CORS and keep credentials server-side
# Uses only stdlib — no requirements.txt needed

from http.server import BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import base64
import urllib.request
import urllib.parse
from datetime import datetime, timezone


def fetch_jira(domain, auth, jql, fields, max_results=100):
    """Fetch issues from Jira using the /search/jql endpoint."""
    params = urllib.parse.urlencode({
        'jql': jql,
        'maxResults': max_results,
        'fields': fields,
    })
    url = f'https://{domain}/rest/api/3/search/jql?{params}'
    req = urllib.request.Request(url, headers={
        'Authorization': f'Basic {auth}',
        'Accept': 'application/json',
    })
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read())


def transform_issue(issue, domain):
    f         = issue['fields']
    is_q1     = '2026-q1' in (f.get('labels') or [])
    assignee  = f.get('assignee') or {}
    issuetype = f.get('issuetype') or {}
    status    = f.get('status') or {}
    priority  = f.get('priority') or {}

    sprints = f.get('customfield_10020') or []
    sprint  = next(
        (s for s in sprints if s.get('state') in ('active', 'future')), None
    )

    # Epic: check parent field (works for both classic and next-gen projects)
    parent        = f.get('parent') or {}
    parent_fields = parent.get('fields') or {}
    parent_type   = (parent_fields.get('issuetype') or {}).get('name', '')
    if parent_type == 'Epic':
        epic_key  = parent.get('key')
        epic_name = parent_fields.get('summary', '')
    elif issuetype.get('name') == 'Epic':
        # The issue itself is an epic
        epic_key  = issue['key']
        epic_name = f.get('summary', '')
    else:
        epic_key  = None
        epic_name = None

    # End date: duedate → sprint end → None (frontend fills in 5 working days)
    end_date = None
    if f.get('duedate'):
        end_date = f['duedate']
    elif sprint and sprint.get('endDate'):
        end_date = sprint['endDate'].split('T')[0]

    return {
        'key':            issue['key'],
        'project':        issue['key'].split('-')[0],
        'summary':        f.get('summary', ''),
        'status':         status.get('name', 'Unknown'),
        'statusCategory': (status.get('statusCategory') or {}).get('key', 'new'),
        'assignee':       assignee.get('displayName', 'Unassigned'),
        'assigneeAvatar': (assignee.get('avatarUrls') or {}).get('48x48'),
        'created':        f.get('created'),
        'updated':        f.get('updated'),
        'startDate':      f.get('created'),
        'endDate':        end_date,
        'duedate':        f.get('duedate'),
        'labels':         f.get('labels') or [],
        'issuetype':      issuetype.get('name', 'Task'),
        'priority':       priority.get('name', 'Unset'),
        'url':            f'https://{domain}/browse/{issue["key"]}',
        'isQ1':           is_q1,
        'sprint':         sprint['name'] if sprint else None,
        'epicKey':        epic_key,
        'epicName':       epic_name,
    }


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        email    = os.environ.get('JIRA_EMAIL', '')
        token    = os.environ.get('JIRA_TOKEN', '')
        domain   = os.environ.get('JIRA_DOMAIN', 'archsys.atlassian.net')
        # Support comma-separated list e.g. "ADT,AAD"
        raw_projects = os.environ.get('JIRA_PROJECTS', os.environ.get('JIRA_PROJECT', 'ADT,AAD'))
        project_list = [p.strip().strip('"') for p in raw_projects.split(',')]
        project_jql  = 'project in (' + ', '.join(f'"{p}"' for p in project_list) + ')'

        if not email or not token:
            self._json(500, {'error': 'Missing JIRA_EMAIL or JIRA_TOKEN env vars'})
            return

        auth   = base64.b64encode(f'{email}:{token}'.encode()).decode()
        fields = (
            'summary,status,assignee,created,updated,'
            'duedate,labels,issuetype,priority,customfield_10020,parent'
        )

        queries = {
            # All Q1-labelled tickets across all projects
            'q1': (
                f'{project_jql} AND labels = "2026-q1" '
                f'ORDER BY updated DESC'
            ),
            # Active tickets (any non-terminal status) updated in Q1 2026
            # Broad exclusion covers both ADT and AAD workflows
            'ip': (
                f'{project_jql} AND status NOT IN '
                f'("Closed", "Done", "Backlog", "Grooming", "Cancelled", "Rejected") '
                f'AND updated >= "2026-01-01" '
                f'AND NOT labels = "2026-q1" ORDER BY updated DESC'
            ),
            # Closed tickets within Q1 2026
            'closed': (
                f'{project_jql} AND status = "Closed" '
                f'AND updated >= "2026-01-01" AND updated <= "2026-03-31" '
                f'AND NOT labels = "2026-q1" ORDER BY updated DESC'
            ),
            # Anything with a due date within Q1 (catch unassigned/backlog items)
            'due': (
                f'{project_jql} AND duedate >= "2026-01-01" '
                f'AND duedate <= "2026-03-31" AND status != "Closed" '
                f'AND NOT labels = "2026-q1" ORDER BY duedate ASC'
            ),
        }

        try:
            # Run all 4 Jira queries in parallel to stay within Vercel's timeout
            results = {}
            with ThreadPoolExecutor(max_workers=4) as executor:
                future_map = {
                    executor.submit(fetch_jira, domain, auth, jql, fields): key
                    for key, jql in queries.items()
                }
                for future in as_completed(future_map):
                    results[future_map[future]] = future.result()

            seen    = set()
            tickets = []
            for key in ('q1', 'ip', 'due', 'closed'):
                for issue in results.get(key, {}).get('issues', []):
                    if issue['key'] not in seen:
                        seen.add(issue['key'])
                        tickets.append(transform_issue(issue, domain))

            self._json(200, {
                'tickets': tickets,
                'meta': {
                    'q1Count':         results.get('q1', {}).get('total', 0),
                    'inProgressCount': results.get('ip', {}).get('total', 0),
                    'closedCount':     results.get('closed', {}).get('total', 0),
                    'dueCount':        results.get('due', {}).get('total', 0),
                    'lastUpdated':     datetime.now(timezone.utc).isoformat(),
                },
            })

        except Exception as exc:
            self._json(502, {'error': str(exc)})

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Cache-Control', 's-maxage=60, stale-while-revalidate=120')

    def _json(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self._cors()
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # suppress default access log noise
