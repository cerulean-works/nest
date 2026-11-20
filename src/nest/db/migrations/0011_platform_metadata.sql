-- 0011: HPC Platform metadata for the environment-bootstrapped Platform Profiles
-- (M1 patch A04).
--
-- Applied migrations are immutable: never edit this file. Change the schema by
-- adding the next numbered file (see "Database migrations" in the docs).
--
-- provider_profiles keeps its name and its rows: each row is one HPC Platform, and
-- its id is the Platform ID that jobs.provider_id references, so the Slurm account
-- of a job is namespaced by (provider_id, account). The two new columns are the
-- operator-supplied metadata from NEST_PLATFORM_<ID>_OPERATOR and _NAME. Both are
-- NULL for profiles defined with NEST_PROVIDERS or NEST_SLURM_*. The Controller
-- refuses to start when the environment would move a recorded Platform ID to a
-- different operator (see nest.db.sync_platforms).

ALTER TABLE provider_profiles
    ADD COLUMN operator text
        CHECK (operator ~ '^[a-z0-9]([a-z0-9._-]{0,62}[a-z0-9])?$'),
    ADD COLUMN display_name text
        CHECK (char_length(display_name) BETWEEN 1 AND 100);
