from datetime import UTC, datetime, timedelta

import httpx

from .models import Schedule
from .provider import ProviderError, receipt


class Engine:
    def __init__(self, store, provider, generator):
        self.store, self.provider, self.generator = store, provider, generator

    def tick(self, now=None):
        now = now or datetime.now(UTC)
        # Reconciliation only reads provider state; never repeats an uncertain publish POST.
        uncertain = sorted((j for j in self.store.jobs(500) if j['state'] == 'unknown'), key=lambda j: j['updated_at'])
        for job in uncertain[:3]:
            try:
                state, url = receipt(self.provider.status(job['id']))
                self.store.finish(job['id'], state, url=url, error=None if url else 'provider_result_pending')
            except (httpx.HTTPError, ProviderError, ValueError):
                self.store.finish(job['id'], 'unknown', error='provider_check_unavailable')
        cfg = self.store.config()
        schedule = Schedule.model_validate(cfg['schedule'])
        if not schedule.enabled or not cfg['profile'] or not cfg['account']:
            return
        if schedule.autonomous_generation:
            for slot in schedule.slots(now):
                if slot <= now + timedelta(minutes=60):
                    self.store.ensure(slot.isoformat(), cfg['revision'])
        jobs = sorted(self.store.jobs(500), key=lambda j: j['slot'])
        for job in jobs:
            slot = datetime.fromisoformat(job['slot'])
            if job['revision'] != cfg['revision'] or job['state'] not in ('queued','ready'):
                continue
            if now > slot + timedelta(minutes=30):
                self.store.finish(job['id'], 'expired', error='publication_window_expired')
                continue
            if job['state'] == 'queued':
                try:
                    account = self.provider.connection()
                    if not account.get('connected') or account['account_id'] != cfg['account']['account_id']:
                        self.store.finish(job['id'], 'blocked', error='account_changed_reconnect_and_confirm')
                        continue
                except (httpx.HTTPError, ProviderError, ValueError):
                    continue
                if slot > now + timedelta(minutes=60) or not self.store.claim(job['id'], 'queued', 'generating'):
                    continue
                try:
                    history = [h for h in self.store.jobs(100) if h['text'] and h['state'] != 'cancelled']
                    text, reference = self.generator.generate(cfg['profile'], history, job['slot'])
                    self.store.finish(job['id'], 'ready', text=text, reference=reference)
                    job['text'] = text
                except (httpx.TimeoutException, httpx.TransportError):
                    self.store.finish(job['id'], 'generation_unknown', error='generation_outcome_unknown')
                    continue
                except (ValueError, KeyError, httpx.HTTPStatusError):
                    self.store.finish(job['id'], 'generation_failed', error='generation_failed_check_settings_or_content')
                    continue
            if slot > now:
                continue
            # A freshly read identity and the revision guard fence stale destination/settings.
            try:
                account = self.provider.connection()
                if not account.get('connected') or account['account_id'] != cfg['account']['account_id']:
                    self.store.finish(job['id'], 'blocked', error='account_changed_reconnect_and_confirm')
                    continue
            except (httpx.HTTPError, ProviderError, ValueError):
                # No publish was dispatched; keep ready for the next tick inside this window.
                continue
            if not self.store.claim(job['id'], 'ready', 'sending'):
                continue
            try:
                state, url = receipt(self.provider.publish(job['id'], job['text']))
                self.store.finish(job['id'], state, url=url, error=None if state == 'published' else 'provider_result_pending')
            except ProviderError as error:
                # A provider can have accepted a request before returning an error.
                self.store.finish(job['id'], 'unknown', error=error.code)
            except (httpx.HTTPError, ValueError):
                self.store.finish(job['id'], 'unknown', error='publish_outcome_unknown')
