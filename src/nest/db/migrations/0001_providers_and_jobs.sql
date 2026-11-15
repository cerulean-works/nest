-- 0001: Provider Profiles bootstrapped from the environment, and Slurm job records.
--
-- Applied migrations are immutable: never edit this file. Change the schema by
-- adding the next numbered file (see "Database migrations" in the docs).

CREATE TABLE provider_profiles (
    id          text PRIMARY KEY CHECK (id ~ '^[a-z][a-z0-9_]{0,31}$'),
    -- Where the profile is defined. Today every profile comes from NEST_PROVIDERS
    -- or NEST_SLURM_*, and the Controller rewrites these rows at startup.
    source      text NOT NULL DEFAULT 'env' CHECK (source IN ('env')),
    ssh_mode    text NOT NULL CHECK (ssh_mode IN ('A', 'B')),
    login_nodes text[] NOT NULL CHECK (cardinality(login_nodes) > 0),
    ssh_user    text,
    account     text NOT NULL,
    partition   text NOT NULL,
    time_limit  text NOT NULL,
    workdir     text,
    is_default  boolean NOT NULL DEFAULT false,
    -- False once the profile is removed from the environment. Rows are kept so
    -- job records keep their provider.
    active      boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX provider_profiles_one_default
    ON provider_profiles (is_default) WHERE is_default;

CREATE TABLE jobs (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    provider_id   text NOT NULL REFERENCES provider_profiles (id),
    -- 'execution': an Execute RPC's worker job. 'batch': a SubmitJob RPC.
    kind          text NOT NULL CHECK (kind IN ('execution', 'batch')),
    -- Nest's view of the request. 'submitting' is written before any SSH call;
    -- 'unknown' means sbatch may have run but no job ID was received.
    status        text NOT NULL DEFAULT 'submitting'
                  CHECK (status IN ('submitting', 'submitted', 'succeeded', 'failed', 'unknown')),
    slurm_job_id  bigint,
    -- Last Slurm state Nest observed, such as PENDING or COMPLETED.
    slurm_state   text,
    request_id    text,
    script        text,
    workdir       text,
    job_dir       text,
    account       text,
    partition     text,
    time_limit    text,
    gpu           integer NOT NULL DEFAULT 0 CHECK (gpu >= 0),
    cpu           integer NOT NULL DEFAULT 0 CHECK (cpu >= 0),
    error_kind    text,
    error_message text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz
);

CREATE INDEX jobs_provider_slurm_job ON jobs (provider_id, slurm_job_id);
CREATE INDEX jobs_created_at ON jobs (created_at);
