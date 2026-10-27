-- Run once after the snapshot is restored. Moves every timestamptz value in the public
-- schema by the same interval, so the newest one is ten minutes old. The dashboard's
-- overview covers the last 24 hours to 30 days, so without this a snapshot made weeks ago
-- would show empty KPIs. Durations, ordering and every CHECK between columns are kept,
-- because each value moves by the same amount. Nothing is live: no worker heartbeats
-- within the liveness window, so the demo shows zero active workers.
DO $$
DECLARE
  col record;
  latest timestamptz;
  newest timestamptz := '-infinity';
  shift interval;
BEGIN
  FOR col IN
    SELECT table_name, column_name FROM information_schema.columns
    WHERE table_schema = 'public' AND data_type = 'timestamp with time zone'
  LOOP
    EXECUTE format('SELECT max(%I) FROM public.%I WHERE %I < ''infinity''',
                   col.column_name, col.table_name, col.column_name) INTO latest;
    IF latest > newest THEN newest := latest; END IF;
  END LOOP;
  IF newest = '-infinity' THEN
    RETURN;
  END IF;
  shift := date_trunc('second', now() - interval '10 minutes' - newest);
  FOR col IN
    SELECT table_name,
           string_agg(format('%1$I = %1$I + %2$L::interval', column_name, shift), ', ') AS sets
    FROM information_schema.columns
    WHERE table_schema = 'public' AND data_type = 'timestamp with time zone'
    GROUP BY table_name
  LOOP
    EXECUTE format('UPDATE public.%I SET %s', col.table_name, col.sets);
  END LOOP;
  RAISE NOTICE 'demo timestamps shifted by %', shift;
END $$;
