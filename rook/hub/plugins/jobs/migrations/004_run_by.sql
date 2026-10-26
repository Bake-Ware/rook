-- Who started a run by hand (access v2 item 8): the caller of the ``run``
-- action when it is not the job's owner, as stored owner_info JSON. Such a
-- run acts as the job's identity on behalf of that caller too, so it never
-- does more than they may. NULL for runs the owner or a trigger started.

ALTER TABLE runs ADD COLUMN run_by TEXT;
