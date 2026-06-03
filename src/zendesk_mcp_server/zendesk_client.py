from typing import Dict, Any, List
import json
import mimetypes
import re
import urllib.request
import urllib.parse
import base64
from datetime import datetime, timedelta
from pathlib import Path
import requests as _requests

from zenpy import Zenpy
from zenpy.lib.api_objects import Comment
from zenpy.lib.api_objects import Ticket as ZenpyTicket


def _slim_ticket(t: dict) -> dict:
    """Return only the fields needed for merge triage."""
    return {
        'id': t.get('id'),
        'subject': t.get('subject'),
        'status': t.get('status'),
        'custom_status_id': t.get('custom_status_id'),
        'created_at': t.get('created_at'),
        'updated_at': t.get('updated_at'),
        'assignee_id': t.get('assignee_id'),
    }


class ZendeskClient:
    def __init__(self, subdomain: str, email: str, token: str):
        """
        Initialize the Zendesk client using zenpy lib and direct API.
        """
        self.client = Zenpy(
            subdomain=subdomain,
            email=email,
            token=token
        )

        # For direct API calls
        self.subdomain = subdomain
        self.email = email
        self.token = token
        self.base_url = f"https://{subdomain}.zendesk.com/api/v2"
        # Create basic auth header
        credentials = f"{email}/token:{token}"
        encoded_credentials = base64.b64encode(credentials.encode()).decode('ascii')
        self.auth_header = f"Basic {encoded_credentials}"

    def _resolve_user_ids(self, ids: List[int]) -> List[Dict[str, Any]]:
        """Bulk-resolve a list of user IDs to {id, name, email} via show_many."""
        if not ids:
            return []
        ids_param = ','.join(str(i) for i in ids)
        url = f"{self.base_url}/users/show_many.json?ids={ids_param}"
        req = urllib.request.Request(url)
        req.add_header('Authorization', self.auth_header)
        req.add_header('Content-Type', 'application/json')
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode())
        return [
            {'id': u.get('id'), 'name': u.get('name'), 'email': u.get('email')}
            for u in data.get('users', [])
        ]

    def get_ticket(self, ticket_id: int, include_comments: bool = False, comment_limit: int = 5) -> Dict[str, Any]:
        """
        Query a ticket by its ID. Optionally embed the most recent comments for triage context.
        """
        try:
            ticket = self.client.tickets(id=ticket_id)
            collaborator_ids = list(getattr(ticket, 'collaborator_ids', []) or [])
            email_cc_ids = list(getattr(ticket, 'email_cc_ids', []) or [])
            all_cc_ids = list({*collaborator_ids, *email_cc_ids})
            resolved = {u['id']: u for u in self._resolve_user_ids(all_cc_ids)} if all_cc_ids else {}
            result = {
                'id': ticket.id,
                'subject': ticket.subject,
                'description': ticket.description,
                'status': ticket.status,
                'custom_status_id': getattr(ticket, 'custom_status_id', None),
                'priority': ticket.priority,
                'created_at': str(ticket.created_at),
                'updated_at': str(ticket.updated_at),
                'requester_id': ticket.requester_id,
                'assignee_id': ticket.assignee_id,
                'organization_id': ticket.organization_id,
                'collaborators': [resolved[i] for i in collaborator_ids if i in resolved],
                'email_ccs': [resolved[i] for i in email_cc_ids if i in resolved],
            }
            if include_comments:
                all_comments = list(self.client.tickets.comments(ticket=ticket_id))
                recent = all_comments[-min(comment_limit, len(all_comments)):]
                lean = []
                for c in recent:
                    body = c.body or ''
                    lean.append({
                        'id': c.id,
                        'author_id': c.author_id,
                        'public': c.public,
                        'created_at': str(c.created_at),
                        'body': body[:500] + ('…' if len(body) > 500 else ''),
                        'attachment_count': len(getattr(c, 'attachments', []) or []),
                    })
                result['recent_comments'] = lean
                result['total_comments'] = len(all_comments)
            return result
        except Exception as e:
            raise Exception(f"Failed to get ticket {ticket_id}: {str(e)}")

    def get_ticket_comments(self, ticket_id: int) -> List[Dict[str, Any]]:
        """
        Get all comments for a specific ticket, including attachment metadata.
        """
        try:
            comments = self.client.tickets.comments(ticket=ticket_id)
            result = []
            for comment in comments:
                attachments = []
                for a in getattr(comment, 'attachments', []) or []:
                    attachments.append({
                        'id': a.id,
                        'file_name': a.file_name,
                        'content_url': a.content_url,
                        'content_type': a.content_type,
                        'size': a.size,
                    })
                result.append({
                    'id': comment.id,
                    'author_id': comment.author_id,
                    'body': comment.body,
                    'public': comment.public,
                    'created_at': str(comment.created_at),
                    'attachments': attachments,
                })
            return result
        except Exception as e:
            raise Exception(f"Failed to get comments for ticket {ticket_id}: {str(e)}")

    # Allowed image MIME types. SVG is excluded — it can contain active XML/JS content.
    _ALLOWED_IMAGE_TYPES = {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}

    # Magic bytes (file signatures) for each allowed type.
    _MAGIC_BYTES: Dict[str, List[bytes]] = {
        'image/jpeg': [b'\xff\xd8\xff'],
        'image/png':  [b'\x89PNG\r\n\x1a\n'],
        'image/gif':  [b'GIF87a', b'GIF89a'],
        'image/webp': [b'RIFF'],  # RIFF....WEBP — checked further below
    }

    # 10 MB hard cap to guard against image bombs and token budget blowout.
    _MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024

    def get_ticket_attachment(self, content_url: str) -> Dict[str, Any]:
        """
        Fetch an image attachment and return base64-encoded data.

        Security measures applied:
        - Allowlist of safe image MIME types (no SVG or arbitrary binary).
        - Magic byte validation so the file header must match the declared type.
        - 10 MB size cap to prevent image bombs and excessive token usage.

        Zendesk attachment URLs redirect to zdusercontent.com (Zendesk's CDN).
        requests strips the Authorization header on cross-origin redirects,
        which is required — the CDN returns 403 if it receives an auth header.
        """
        try:
            response = _requests.get(
                content_url,
                headers={'Authorization': self.auth_header},
                timeout=30,
                stream=True,
            )
            response.raise_for_status()

            content_type = response.headers.get('Content-Type', '').split(';')[0].strip().lower()

            if content_type not in self._ALLOWED_IMAGE_TYPES:
                raise ValueError(
                    f"Attachment type '{content_type}' is not allowed. "
                    f"Supported types: {sorted(self._ALLOWED_IMAGE_TYPES)}"
                )

            # Read with size cap — stops download as soon as limit is exceeded.
            chunks = []
            total = 0
            for chunk in response.iter_content(chunk_size=65536):
                total += len(chunk)
                if total > self._MAX_ATTACHMENT_BYTES:
                    raise ValueError(
                        f"Attachment exceeds the {self._MAX_ATTACHMENT_BYTES // (1024*1024)} MB size limit."
                    )
                chunks.append(chunk)
            content = b''.join(chunks)

            # Validate magic bytes to catch MIME type spoofing.
            magic_signatures = self._MAGIC_BYTES.get(content_type, [])
            if magic_signatures and not any(content.startswith(sig) for sig in magic_signatures):
                raise ValueError(
                    f"File header does not match declared content type '{content_type}'. "
                    "The attachment may be spoofed."
                )
            # Extra check for WebP: bytes 8–12 must be b'WEBP'.
            if content_type == 'image/webp' and content[8:12] != b'WEBP':
                raise ValueError("File header does not match declared content type 'image/webp'.")

            return {
                'data': base64.b64encode(content).decode('ascii'),
                'content_type': content_type,
            }
        except (ValueError, _requests.HTTPError):
            raise
        except Exception as e:
            raise Exception(f"Failed to fetch attachment from {content_url}: {str(e)}")

    def post_comment(self, ticket_id: int, comment: str, public: bool = True, uploads: List[str] | None = None) -> str:
        """
        Post a comment to an existing ticket.
        Uses direct REST when uploads are provided (zenpy Comment doesn't surface that field).
        """
        if uploads:
            return self._post_comment_rest(ticket_id, comment, public, uploads)
        try:
            ticket = self.client.tickets(id=ticket_id)
            ticket.comment = Comment(
                html_body=comment,
                public=public
            )
            self.client.tickets.update(ticket)
            return comment
        except Exception as e:
            raise Exception(f"Failed to post comment on ticket {ticket_id}: {str(e)}")

    def _post_comment_rest(self, ticket_id: int, comment: str, public: bool, uploads: List[str]) -> str:
        """Direct REST PUT for comments that include file attachment tokens."""
        try:
            url = f"{self.base_url}/tickets/{ticket_id}.json"
            payload = {
                'ticket': {
                    'comment': {
                        'html_body': comment,
                        'public': public,
                        'uploads': uploads,
                    }
                }
            }
            response = _requests.put(
                url,
                headers={
                    'Authorization': self.auth_header,
                    'Content-Type': 'application/json',
                },
                json=payload,
                timeout=30,
            )
            response.raise_for_status()
            return comment
        except _requests.HTTPError as e:
            error_body = e.response.text if e.response is not None else "No response body"
            status_code = e.response.status_code if e.response is not None else "unknown"
            raise Exception(f"Failed to post comment on ticket {ticket_id}: HTTP {status_code} - {error_body}")
        except Exception as e:
            raise Exception(f"Failed to post comment on ticket {ticket_id}: {str(e)}")

    def get_tickets(self, page: int = 1, per_page: int = 25, sort_by: str = 'created_at', sort_order: str = 'desc') -> Dict[str, Any]:
        """
        Get the latest tickets with proper pagination support using direct API calls.

        Args:
            page: Page number (1-based)
            per_page: Number of tickets per page (max 100)
            sort_by: Field to sort by (created_at, updated_at, priority, status)
            sort_order: Sort order (asc or desc)

        Returns:
            Dict containing tickets and pagination info
        """
        try:
            # Cap at reasonable limit
            per_page = min(per_page, 100)

            # Build URL with parameters for offset pagination
            params = {
                'page': str(page),
                'per_page': str(per_page),
                'sort_by': sort_by,
                'sort_order': sort_order
            }
            query_string = urllib.parse.urlencode(params)
            url = f"{self.base_url}/tickets.json?{query_string}"

            # Create request with auth header
            req = urllib.request.Request(url)
            req.add_header('Authorization', self.auth_header)
            req.add_header('Content-Type', 'application/json')

            # Make the API request
            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read().decode())

            tickets_data = data.get('tickets', [])

            # Process tickets to return only essential fields
            ticket_list = []
            for ticket in tickets_data:
                desc = ticket.get('description') or ''
                ticket_list.append({
                    'id': ticket.get('id'),
                    'subject': ticket.get('subject'),
                    'status': ticket.get('status'),
                    'custom_status_id': ticket.get('custom_status_id'),
                    'priority': ticket.get('priority'),
                    'description': desc[:200] + ('...' if len(desc) > 200 else ''),
                    'created_at': ticket.get('created_at'),
                    'updated_at': ticket.get('updated_at'),
                    'requester_id': ticket.get('requester_id'),
                    'assignee_id': ticket.get('assignee_id')
                })

            return {
                'tickets': ticket_list,
                'page': page,
                'per_page': per_page,
                'count': len(ticket_list),
                'sort_by': sort_by,
                'sort_order': sort_order,
                'has_more': data.get('next_page') is not None,
                'next_page': page + 1 if data.get('next_page') else None,
                'previous_page': page - 1 if data.get('previous_page') and page > 1 else None
            }
        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else "No response body"
            raise Exception(f"Failed to get latest tickets: HTTP {e.code} - {e.reason}. {error_body}")
        except Exception as e:
            raise Exception(f"Failed to get latest tickets: {str(e)}")

    def get_all_articles(self) -> Dict[str, Any]:
        """
        Fetch help center articles as knowledge base.
        Returns a Dict of section -> [article].
        """
        try:
            # Get all sections
            sections = self.client.help_center.sections()

            # Get articles for each section
            kb = {}
            for section in sections:
                articles = self.client.help_center.sections.articles(section.id)
                kb[section.name] = {
                    'section_id': section.id,
                    'description': section.description,
                    'articles': [{
                        'id': article.id,
                        'title': article.title,
                        'body': article.body,
                        'updated_at': str(article.updated_at),
                        'url': article.html_url
                    } for article in articles]
                }

            return kb
        except Exception as e:
            raise Exception(f"Failed to fetch knowledge base: {str(e)}")

    def create_ticket(
        self,
        subject: str,
        description: str,
        requester_id: int | None = None,
        assignee_id: int | None = None,
        priority: str | None = None,
        type: str | None = None,
        tags: List[str] | None = None,
        custom_fields: List[Dict[str, Any]] | None = None,
    ) -> Dict[str, Any]:
        """
        Create a new Zendesk ticket using Zenpy and return essential fields.

        Args:
            subject: Ticket subject
            description: Ticket description (plain text). Will also be used as initial comment.
            requester_id: Optional requester user ID
            assignee_id: Optional assignee user ID
            priority: Optional priority (low, normal, high, urgent)
            type: Optional ticket type (problem, incident, question, task)
            tags: Optional list of tags
            custom_fields: Optional list of dicts: {id: int, value: Any}
        """
        try:
            ticket = ZenpyTicket(
                subject=subject,
                description=description,
                requester_id=requester_id,
                assignee_id=assignee_id,
                priority=priority,
                type=type,
                tags=tags,
                custom_fields=custom_fields,
            )
            created_audit = self.client.tickets.create(ticket)
            t = getattr(created_audit, 'ticket', None)
            return {
                'id': getattr(t, 'id', None),
                'subject': getattr(t, 'subject', subject),
                'description': getattr(t, 'description', description),
                'status': getattr(t, 'status', 'new'),
                'custom_status_id': getattr(t, 'custom_status_id', None),
                'priority': getattr(t, 'priority', priority),
                'type': getattr(t, 'type', type),
                'created_at': str(getattr(t, 'created_at', '')),
                'updated_at': str(getattr(t, 'updated_at', '')),
                'requester_id': getattr(t, 'requester_id', requester_id),
                'assignee_id': getattr(t, 'assignee_id', assignee_id),
                'organization_id': getattr(t, 'organization_id', None),
                'tags': list(getattr(t, 'tags', tags or []) or []),
            }
        except Exception as e:
            raise Exception(f"Failed to create ticket: {str(e)}")

    def merge_tickets(
        self,
        target_ticket_id: int,
        source_ticket_ids: List[int],
        target_comment: str | None = None,
        source_comment: str | None = None,
    ) -> Dict[str, Any]:
        """
        Merge one or more source tickets into a target ticket via Zendesk's native merge endpoint.
        Source tickets are closed; the target ticket survives. Returns a job_status dict.
        """
        try:
            url = f"{self.base_url}/tickets/{target_ticket_id}/merge.json"
            payload: Dict[str, Any] = {"ids": source_ticket_ids}
            if target_comment is not None:
                payload["target_comment"] = target_comment
            if source_comment is not None:
                payload["source_comment"] = source_comment

            response = _requests.post(
                url,
                headers={
                    "Authorization": self.auth_header,
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30,
            )
            response.raise_for_status()
            return response.json()
        except _requests.HTTPError as e:
            error_body = e.response.text if e.response is not None else "No response body"
            status_code = e.response.status_code if e.response is not None else "unknown"
            raise Exception(
                f"Failed to merge tickets into {target_ticket_id}: HTTP {status_code} - {error_body}"
            )
        except Exception as e:
            raise Exception(f"Failed to merge tickets into {target_ticket_id}: {str(e)}")

    def get_job_status(self, job_id: str) -> Dict[str, Any]:
        """
        Poll the status of an async Zendesk job (e.g. a ticket merge) by job ID.
        """
        try:
            url = f"{self.base_url}/job_statuses/{job_id}.json"
            response = _requests.get(
                url,
                headers={"Authorization": self.auth_header},
                timeout=30,
            )
            response.raise_for_status()
            return response.json()
        except _requests.HTTPError as e:
            error_body = e.response.text if e.response is not None else "No response body"
            status_code = e.response.status_code if e.response is not None else "unknown"
            raise Exception(
                f"Failed to get job status for {job_id}: HTTP {status_code} - {error_body}"
            )
        except Exception as e:
            raise Exception(f"Failed to get job status for {job_id}: {str(e)}")

    def search_tickets(self, query: str, page: int = 1, per_page: int = 100) -> Dict[str, Any]:
        """
        Search tickets using Zendesk's Search API.

        The query uses Zendesk search syntax, e.g.:
          type:ticket assignee:none status:open
          type:ticket status:open created>2024-01-01
        """
        try:
            per_page = min(per_page, 100)
            params = {
                'query': query,
                'page': str(page),
                'per_page': str(per_page),
            }
            query_string = urllib.parse.urlencode(params)
            url = f"{self.base_url}/search.json?{query_string}"

            req = urllib.request.Request(url)
            req.add_header('Authorization', self.auth_header)
            req.add_header('Content-Type', 'application/json')

            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read().decode())

            results = []
            for item in data.get('results', []):
                if item.get('result_type') != 'ticket':
                    continue
                desc = item.get('description') or ''
                results.append({
                    'id': item.get('id'),
                    'subject': item.get('subject'),
                    'status': item.get('status'),
                    'custom_status_id': item.get('custom_status_id'),
                    'priority': item.get('priority'),
                    'description': desc[:200] + ('...' if len(desc) > 200 else ''),
                    'created_at': item.get('created_at'),
                    'updated_at': item.get('updated_at'),
                    'requester_id': item.get('requester_id'),
                    'assignee_id': item.get('assignee_id'),
                    'organization_id': item.get('organization_id'),
                })

            return {
                'tickets': results,
                'count': data.get('count', len(results)),
                'page': page,
                'per_page': per_page,
                'has_more': data.get('next_page') is not None,
                'next_page': page + 1 if data.get('next_page') else None,
            }
        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else "No response body"
            raise Exception(f"Failed to search tickets: HTTP {e.code} - {e.reason}. {error_body}")
        except Exception as e:
            raise Exception(f"Failed to search tickets: {str(e)}")

    def list_organizations(self) -> List[Dict[str, Any]]:
        try:
            orgs = []
            page = 1
            while True:
                url = f"{self.base_url}/organizations.json?page={page}&per_page=100"
                req = urllib.request.Request(url)
                req.add_header('Authorization', self.auth_header)
                req.add_header('Content-Type', 'application/json')
                with urllib.request.urlopen(req) as response:
                    data = json.loads(response.read().decode())
                for org in data.get('organizations', []):
                    orgs.append({
                        'id': org.get('id'),
                        'name': org.get('name'),
                        'domain_names': org.get('domain_names', []),
                    })
                if not data.get('next_page'):
                    break
                page += 1
            return orgs
        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else "No response body"
            raise Exception(f"Failed to list organizations: HTTP {e.code} - {e.reason}. {error_body}")
        except Exception as e:
            raise Exception(f"Failed to list organizations: {str(e)}")

    def get_custom_statuses(self) -> List[Dict[str, Any]]:
        """
        Return all custom ticket statuses defined in the account.
        """
        try:
            url = f"{self.base_url}/custom_statuses.json"
            req = urllib.request.Request(url)
            req.add_header('Authorization', self.auth_header)
            req.add_header('Content-Type', 'application/json')
            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read().decode())
            return [
                {
                    'id': s.get('id'),
                    'agent_label': s.get('agent_label'),
                    'status_category': s.get('status_category'),
                    'active': s.get('active'),
                    'default': s.get('default'),
                }
                for s in data.get('custom_statuses', [])
            ]
        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else "No response body"
            raise Exception(f"Failed to get custom statuses: HTTP {e.code} - {e.reason}. {error_body}")
        except Exception as e:
            raise Exception(f"Failed to get custom statuses: {str(e)}")

    def find_merge_candidates(self, lookback_days: int = 90) -> List[Dict[str, Any]]:
        """
        Find standing tickets that new unassigned tickets should be merged into.

        Runs 2-3 API calls total: one for new tickets, one batch fetch of all
        standing tickets (paginated up to 300), then matching is done in-process.
        Previously made N+1 serial API calls.
        """
        lookback_date = (datetime.utcnow() - timedelta(days=lookback_days)).strftime('%Y-%m-%d')

        new_result = self.search_tickets('type:ticket status:new assignee:none', per_page=100)
        new_tickets = new_result.get('tickets', [])
        if not new_tickets:
            return []

        new_ids = {t['id'] for t in new_tickets}

        # Single broad fetch of all standing tickets — paginate up to 300
        standing: List[Dict] = []
        for page in range(1, 4):
            result = self.search_tickets(
                f'type:ticket updated>{lookback_date} -status:new -status:solved -status:closed',
                page=page, per_page=100,
            )
            standing.extend(result.get('tickets', []))
            if not result.get('has_more'):
                break

        # Match every new ticket against the standing set locally
        results = []
        for ticket in new_tickets:
            subject = ticket.get('subject', '')
            words = re.split(r'[\s\-_/\\|,;:!?()[\]{}<>=]+', subject)
            all_terms = [w.lower() for w in words if len(w) >= 3 and not w.isdigit()]

            if not all_terms:
                results.append({'new_ticket': _slim_ticket(ticket), 'candidates': []})
                continue

            scored = []
            for t in standing:
                if t['id'] in new_ids:
                    continue
                cand_words = set(re.split(
                    r'[\s\-_/\\|,;:!?()[\]{}<>=]+',
                    (t.get('subject') or '').lower()
                ))
                matches = sum(1 for term in all_terms if term in cand_words)
                overlap = matches / len(all_terms) if all_terms else 0.0
                if matches >= 2 or overlap >= 0.25:
                    slim = _slim_ticket(t)
                    slim['match_score'] = round(overlap, 2)
                    slim['match_terms'] = matches
                    scored.append(slim)

            scored.sort(key=lambda c: (-c['match_score'], c.get('updated_at', '') or ''))
            results.append({
                'new_ticket': _slim_ticket(ticket),
                'candidates': scored[:5],
            })

        return results

    def lookup_user(self, email: str) -> Dict[str, Any]:
        """
        Look up a Zendesk user by email address.
        Returns user details if found, or found=False if the user does not exist.
        """
        try:
            params = {'query': f'email:{email}'}
            query_string = urllib.parse.urlencode(params)
            url = f"{self.base_url}/users/search.json?{query_string}"

            req = urllib.request.Request(url)
            req.add_header('Authorization', self.auth_header)
            req.add_header('Content-Type', 'application/json')

            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read().decode())

            users = data.get('users', [])
            if not users:
                return {'found': False, 'email': email}

            user = users[0]
            return {
                'found': True,
                'id': user.get('id'),
                'name': user.get('name'),
                'email': user.get('email'),
                'role': user.get('role'),
                'active': user.get('active'),
            }
        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else "No response body"
            raise Exception(f"Failed to look up user {email}: HTTP {e.code} - {e.reason}. {error_body}")
        except Exception as e:
            raise Exception(f"Failed to look up user {email}: {str(e)}")

    def upload_file(self, local_path: str, filename: str | None = None) -> Dict[str, Any]:
        """
        Upload a local file to Zendesk and return the attachment token.
        The token can be passed in the uploads array when posting a comment.
        """
        source = Path(local_path).expanduser()
        if not source.exists():
            raise FileNotFoundError(f"File not found: {local_path!r}")
        if not source.is_file():
            raise ValueError(f"Path is not a file: {local_path!r}")

        if filename is None:
            filename = source.name

        content = source.read_bytes()
        content_type, _ = mimetypes.guess_type(filename)
        if not content_type:
            content_type = 'application/octet-stream'

        params = urllib.parse.urlencode({'filename': filename})
        url = f"{self.base_url}/uploads.json?{params}"

        try:
            response = _requests.post(
                url,
                headers={
                    'Authorization': self.auth_header,
                    'Content-Type': content_type,
                },
                data=content,
                timeout=60,
            )
            response.raise_for_status()
            upload = response.json().get('upload', {})
            return {
                'token': upload.get('token'),
                'expires_at': upload.get('expires_at'),
                'filename': filename,
            }
        except _requests.HTTPError as e:
            error_body = e.response.text if e.response is not None else "No response body"
            status_code = e.response.status_code if e.response is not None else "unknown"
            raise Exception(f"Failed to upload file: HTTP {status_code} - {error_body}")
        except Exception as e:
            raise Exception(f"Failed to upload file: {str(e)}")

    def update_ticket(self, ticket_id: int, **fields: Any) -> Dict[str, Any]:
        """
        Update a Zendesk ticket. Uses direct REST when custom_status_id is present
        (zenpy may not serialize that field); otherwise uses zenpy.
        """
        if any(k in fields for k in ('custom_status_id', 'collaborator_ids', 'email_cc_ids')):
            return self._update_ticket_rest(ticket_id, **fields)

        try:
            # Load the ticket, mutate fields directly, and update
            ticket = self.client.tickets(id=ticket_id)
            for key, value in fields.items():
                if value is None:
                    continue
                setattr(ticket, key, value)

            # This call returns a TicketAudit (not a Ticket). Don't read attrs from it.
            self.client.tickets.update(ticket)

            # Fetch the fresh ticket to return consistent data
            refreshed = self.client.tickets(id=ticket_id)

            return {
                'id': refreshed.id,
                'subject': refreshed.subject,
                'description': refreshed.description,
                'status': refreshed.status,
                'priority': refreshed.priority,
                'type': getattr(refreshed, 'type', None),
                'created_at': str(refreshed.created_at),
                'updated_at': str(refreshed.updated_at),
                'requester_id': refreshed.requester_id,
                'assignee_id': refreshed.assignee_id,
                'organization_id': refreshed.organization_id,
                'tags': list(getattr(refreshed, 'tags', []) or []),
            }
        except Exception as e:
            raise Exception(f"Failed to update ticket {ticket_id}: {str(e)}")

    def _update_ticket_rest(self, ticket_id: int, **fields: Any) -> Dict[str, Any]:
        """Direct REST PUT for ticket updates that include fields zenpy may not serialize."""
        try:
            url = f"{self.base_url}/tickets/{ticket_id}.json"
            payload = {'ticket': {k: v for k, v in fields.items() if v is not None}}
            response = _requests.put(
                url,
                headers={
                    'Authorization': self.auth_header,
                    'Content-Type': 'application/json',
                },
                json=payload,
                timeout=30,
            )
            response.raise_for_status()
            t = response.json().get('ticket', {})
            return {
                'id': t.get('id'),
                'subject': t.get('subject'),
                'description': t.get('description'),
                'status': t.get('status'),
                'priority': t.get('priority'),
                'type': t.get('type'),
                'created_at': t.get('created_at'),
                'updated_at': t.get('updated_at'),
                'requester_id': t.get('requester_id'),
                'assignee_id': t.get('assignee_id'),
                'organization_id': t.get('organization_id'),
                'tags': list(t.get('tags', []) or []),
                'custom_status_id': t.get('custom_status_id'),
                'collaborator_ids': t.get('collaborator_ids', []),
                'email_cc_ids': t.get('email_cc_ids', []),
            }
        except _requests.HTTPError as e:
            error_body = e.response.text if e.response is not None else "No response body"
            status_code = e.response.status_code if e.response is not None else "unknown"
            raise Exception(f"Failed to update ticket {ticket_id}: HTTP {status_code} - {error_body}")
        except Exception as e:
            raise Exception(f"Failed to update ticket {ticket_id}: {str(e)}")