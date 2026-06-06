-- UI instrumentation readout. DB: ~/.config/periscope/periscope.db, table ui_events.
-- WHERE dev=0 excludes events logged by a dev instance (PORT != 8765).
-- NEVER COUNT/SUM across the api:<label> namespace and the dotted gesture
-- namespace as one total — group within a namespace, or compare deliberately.

-- Top actions, all time (real usage only)
SELECT name, COUNT(*) n FROM ui_events WHERE dev=0
GROUP BY name ORDER BY n DESC;

-- Last 7 days
SELECT name, COUNT(*) n FROM ui_events
WHERE dev=0 AND at > strftime('%s','now','-7 days')
GROUP BY name ORDER BY n DESC;

-- Where do I rename from? (gesture vs effect — both namespaces on purpose)
SELECT name, COUNT(*) FROM ui_events
WHERE dev=0 AND name LIKE '%rename%' GROUP BY name;

-- Daily volume
SELECT date(at,'unixepoch','localtime') d, COUNT(*) n
FROM ui_events WHERE dev=0 GROUP BY d ORDER BY d DESC;

-- Sessions per day (app.open heartbeat)
SELECT date(at,'unixepoch','localtime') d, COUNT(*) sessions
FROM ui_events WHERE dev=0 AND name='app.open' GROUP BY d ORDER BY d DESC;
