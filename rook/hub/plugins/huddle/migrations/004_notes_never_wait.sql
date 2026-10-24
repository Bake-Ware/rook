-- Note cards never wait on the person (2026-10-23, Bake: approval asks he
-- could not read, two of them role notes days old). A note runs nothing, so
-- it is recorded as noted when proposed; the ones still waiting from before
-- are settled here the same way. Released: never edit this file.
UPDATE huddle_cards
   SET state = 'done',
       result = '{"ok": true, "noted": true}',
       decided_by = 'hub: notes never wait (2026-10-23)',
       decided = strftime('%s', 'now')
 WHERE kind = 'note' AND state = 'proposed';
