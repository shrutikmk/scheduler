Habit Builder — optional disk copies

Habit Builder keeps data in the browser (IndexedDB + localStorage) or, when served by the day-scheduler UI, in SQLite via the server API. The page no longer offers Export/Import backup buttons.

You may still store legacy habit-builder-backup-*.json files here yourself if you created them before that UI was removed.

Tracked exports are not committed by default (see root .gitignore: backups/*.json).
