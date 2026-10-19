"""Inbox sync: classify application emails, match them to tracked jobs, move statuses.

The /jobs-sync skill fetches Gmail threads and writes one JSON per thread to data/mail_inbox/.
Everything from that point on is deterministic Python: classify -> match -> transitions.
"""
