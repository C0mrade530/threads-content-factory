from urllib.parse import urlsplit
import httpx


class ProviderError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class UploadPost:
    def __init__(self, key, profile, *, transport=None):
        self.key, self.profile = key, profile
        self.client = httpx.Client(base_url="https://api.upload-post.com", timeout=40,
            headers={"Authorization": "Apikey " + key}, transport=transport, follow_redirects=False)

    def close(self):
        self.client.close()

    def request(self, method, path, **kwargs):
        if not self.key:
            raise ProviderError("upload_post_not_configured")
        response = self.client.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise ProviderError("upload_post_http_" + str(response.status_code))
        return response.json()

    def profiles(self):
        body = self.request("GET", "/api/uploadposts/users")
        profiles = body.get("profiles") if isinstance(body, dict) else None
        if not isinstance(profiles, list) or any(not isinstance(p, dict) for p in profiles):
            raise ProviderError("invalid_profiles_response")
        return profiles

    def connection(self):
        profiles = [p for p in self.profiles() if p.get("username") == self.profile]
        accounts = profiles[0].get("social_accounts") if profiles else None
        raw = accounts.get("threads") if isinstance(accounts, dict) else None
        if not isinstance(raw, dict) or not raw.get("username") or raw.get("reauth_required"):
            return {"connected": False, "profile": self.profile}
        return {"connected": True, "profile": self.profile, "account_id": str(raw['username']),
                "handle": raw.get("handle") or raw.get("display_name") or str(raw['username'])}

    def connect_url(self):
        if not any(p.get('username') == self.profile for p in self.profiles()):
            self.request("POST", "/api/uploadposts/users", json={"username": self.profile, "display_name": "Threads контент завод"})
        result = self.request("POST", "/api/uploadposts/users/generate-jwt", json={
            "username": self.profile, "platforms": ["threads"], "show_calendar": False,
            "connect_title": "Threads контент завод", "connect_description": "Подключите Threads для публикации ваших постов"})
        url = result.get('access_url', '') if isinstance(result, dict) else ''
        if not isinstance(url, str):
            raise ProviderError("invalid_connect_url")
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname != 'app.upload-post.com' or parsed.username or parsed.password:
            raise ProviderError("invalid_connect_url")
        return {"url": url, "duration": result.get('duration'), "instruction": "Откройте ссылку, войдите в свой Threads. Затем проверьте и подтвердите аккаунт в чате."}

    def publish(self, identity, text):
        return self.request("POST", "/api/upload_text", headers={"Idempotency-Key": identity, "X-Request-Id": identity},
            data={"user": self.profile, "platform[]": "threads", "title": text,
                  "async_upload": "true", "request_id": identity, "external_id": identity})

    def status(self, identity):
        return self.request("GET", "/api/uploadposts/status", params={"request_id": identity})


def receipt(payload):
    if not isinstance(payload, dict):
        return 'unknown', None
    results = payload.get('results', {})
    if isinstance(results, list):
        result = next((r for r in results if isinstance(r, dict) and r.get('platform') == 'threads'), {})
    elif isinstance(results, dict):
        result = results.get('threads', {})
    else:
        return 'unknown', None
    if not isinstance(result, dict):
        return 'unknown', None
    if result.get('success') is True:
        url = result.get('url') or result.get('post_url')
        if not isinstance(url, str):
            return 'unknown', None
        parsed = urlsplit(url or '')
        if parsed.scheme == 'https' and parsed.hostname in {'threads.net', 'www.threads.net', 'threads.com', 'www.threads.com'} and not parsed.username and not parsed.password and '/post/' in parsed.path:
            return 'published', url
    if result.get('skipped') or result.get('status') == 'failed' or result.get('success') is False:
        return 'blocked', None
    return 'unknown', None
