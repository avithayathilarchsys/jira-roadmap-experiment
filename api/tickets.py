# api/tickets.py — Vercel Serverless Function (Python)
# Proxies Jira API to avoid CORS and keep credentials server-side
# Uses only stdlib — no requirements.txt needed

from http.server import BaseHTTPRequestHandler
import json
import os
import base64
import urllib.request
import urllib.parse
from datetime import datetime, timezone


def fetch_jira(domain, auth, jql, fields, max_results=100):
    params = urllib.parse.urlencode({
        'jql': jql,
        'maxResults': max_results,
        'fields': fields,
        'expand': 'changelog',
    })
    url = f'https://{domain}/rest/api/3/search?{params}'
    req = urllib.request.Request(url, headers={
        'Authorization': f'Basic {auth}',
        'Accept': 'application/json',
    })
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def get_in_progress_date(changelog, created):
    """Walk the changelog to find when the ticket moved to In Progress."""
    if not changelog or 'histories' not in changelog:
        return created
    for history in reversed(changelog['histories']):
        for item in history.get('items', []):
            if (
                item.get('field') == 'status'
                and 'in progress' in (item.get('toString') or '').lower()
            ):
                return history['created']
    return created


def transform_issue(issue, domain):
    f = issue['fields']
    is_q1 = '2026-q1' in (f.get('labels') or [])
    start_date = get_in_progress_date(issue.get('changelog'), f.get('created'))

    sprints = f.get('customfield_10020') or []
    sprint = next(
        (s for s in sprints if s.get('state') in ('active', 'future')), None
    )

    end_date = None
    if f.get('duedate'):
        end_date = f['duedate']
    elif sprint and sprint.get('endDate'):
        end_date = sprint['endDate'].split('T')[0]

    assignee  = f.get('assignee') or {}
    issuetype = f.get('issuetype') or {}
    status    = f.get('status') or {}
    priority  = f.get('priority') or {}

    return {
        'key':            issue['key'],
        'summary':        f.get('summary', ''),
        'status':         status.get('name', 'Unknown'),
        'statusCategory': (status.get('statusCategory') or {}).get('key', 'new'),
        'assignee':       assignee.get('displayName', 'Unassigned'),
        'assigneeAvatar': (assignee.get('avatarUrls') or {}).get('48x48'),
        'created':        f.get('created'),
        'updated':        f.get('updated'),
        'startDate':      start_date,
        'endDate':        end_date,
        'duedate':        f.get('duedate'),
        'labels':         f.get('labels') or [],
        'issuetype':      issuetype.get('name', 'Task'),
        'priority':       priority.get('name', 'Unset'),
        'url':            f'https://{domain}/browse/{issue["key"]}',
        'isQ1':           is_q1,
        'sprint':         sprint['name'] if sprint else None,
    }


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        email   = os.environ.get('JIRA_EMAIL', '')
        token   = os.environ.get('JIRA_TOKEN', '')
        domain  = os.environ.get('JIRA_DOMAIN', 'archsys.atlassian.net')
        project = os.environ.get('JIRA_PROJECT', 'ADT')

        if not email or not token:
            self._json(500, {'error': 'Missing JIRA_EMAIL or JIRA_TOKEN env vars'})
            return

        auth   = base64.b64encode(f'{email}:{token}'.encode()).decode()
        fields = (
            'summary,status,assignee,created,updated,'
            'duedate,labels,issuetype,priority,customfield_10020'
        )

        try:
            q1_jql = f'project = "{project}" AND labels = "2026-q1" ORDER BY created ASC'
            ip_jql = (
                f'project = "{project}" AND status = "In Progress" '
                f'AND NOT labels = "2026-q1" ORDER BY created ASC'
            )

            q1_data = fetch_jira(domain, auth, q1_jql, fields)
            ip_data = fetch_jira(domain, auth, ip_jql, fields)

            seen    = set()
            tickets = []
            for issue in q1_data.get('issues', []) + ip_data.get('issues', []):
                if issue['key'] not in seen:
                    seen.add(issue['key'])
                    tickets.append(transform_issue(issue, domain))

            self._json(200, {
                'tickets': tickets,
                'meta': {
                    'q1Count':          q1_data.get('total', 0),
                    'inProgressCount':  ip_data.get('total', 0),
                    'lastUpdated':      datetime.now(timezone.utc).isoformat(),
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
