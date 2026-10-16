-- firstthings: initial schema.
-- Lives in its own schema "ft", which Supabase does not expose through its public API.

create schema if not exists ft;

create table ft.roles (
  key   text primary key,
  name  text not null,
  sort  int  not null default 0
);

create table ft.activity_types (
  key             text primary key,
  name            text not null,
  role_key        text not null references ft.roles(key),
  default_minutes int  not null,
  min_minutes     int,
  unit            text not null check (unit in ('session', 'minutes', 'count')),
  flexibility     text not null check (flexibility in ('fixed', 'movable', 'droppable')),
  quadrant        smallint check (quadrant between 1 and 4),
  protected       boolean not null default false,
  quiet           boolean not null default false,
  confirm         text not null default 'ask' check (confirm in ('ask', 'auto')),
  is_sleep        boolean not null default false
);

create table ft.weeks (
  id                   bigint generated always as identity primary key,
  start_date           date not null unique check (extract(isodow from start_date) = 1),
  status               text not null default 'active' check (status in ('draft', 'active', 'closed')),
  baseline             boolean not null default false,
  sleep_target_minutes int  not null,
  template_hash        text,
  created_at           timestamptz not null default now(),
  notes                text
);

create table ft.weekly_targets (
  id           bigint generated always as identity primary key,
  week_id      bigint not null references ft.weeks(id) on delete cascade,
  activity_key text   not null references ft.activity_types(key),
  floor        int    not null,
  goal         int    not null,
  priority     smallint not null default 2,
  provisional  boolean not null default true,
  unique (week_id, activity_key),
  check (goal >= floor)
);

create table ft.role_goals (
  id       bigint generated always as identity primary key,
  week_id  bigint not null references ft.weeks(id) on delete cascade,
  role_key text   not null references ft.roles(key),
  text     text   not null,
  status   text   not null default 'open' check (status in ('open', 'done', 'dropped'))
);

-- Every replan and its reason (written from Phase 2).
create table ft.plan_changes (
  id                 bigint generated always as identity primary key,
  week_id            bigint not null references ft.weeks(id) on delete cascade,
  created_at         timestamptz not null default now(),
  trigger_message_id bigint,
  situation          jsonb,
  operations         jsonb not null default '[]',
  rationale          text,
  status             text not null check (status in ('proposed', 'applied', 'undone', 'rejected')),
  applied_at         timestamptz,
  undone_at          timestamptz
);

-- The ledger. Append-only in spirit: a move marks the old row 'moved' and creates a successor.
create table ft.plan_items (
  id                   bigint generated always as identity primary key,
  week_id              bigint not null references ft.weeks(id) on delete cascade,
  day                  date   not null,
  activity_key         text   not null references ft.activity_types(key),
  role_key             text   not null references ft.roles(key),
  title                text   not null,
  start_at             timestamptz not null,
  end_at               timestamptz not null,
  planned_minutes      int    not null check (planned_minutes > 0),
  priority             smallint not null default 2,
  quadrant             smallint check (quadrant between 1 and 4),
  status               text not null default 'planned'
                       check (status in ('planned', 'done', 'partial', 'skipped', 'moved', 'cancelled', 'unconfirmed')),
  actual_minutes       int check (actual_minutes >= 0),
  resolved_at          timestamptz,
  on_track_at          timestamptz,
  skip_reason_category text check (skip_reason_category in ('constraint', 'capacity', 'preference', 'reprioritised')),
  skip_reason_text     text,
  moved_from_id        bigint references ft.plan_items(id),
  postponement_count   int  not null default 0,
  origin               text not null default 'template' check (origin in ('template', 'sunday', 'replan', 'ad_hoc')),
  change_id            bigint references ft.plan_changes(id),
  notes                text,
  created_at           timestamptz not null default now(),
  check (end_at > start_at)
);
create index plan_items_day on ft.plan_items (day);
create index plan_items_week_activity on ft.plan_items (week_id, activity_key);

-- Every change to an item's outcome, for audit and /undo.
create table ft.item_events (
  id        bigint generated always as identity primary key,
  item_id   bigint not null references ft.plan_items(id) on delete cascade,
  at        timestamptz not null default now(),
  source    text not null check (source in ('button', 'command', 'day_close', 'replan', 'conversation')),
  before    jsonb not null,
  after     jsonb not null,
  undone_at timestamptz
);

-- Unplanned things that took time (written from Phase 2).
create table ft.disruptions (
  id                 bigint generated always as identity primary key,
  week_id            bigint not null references ft.weeks(id) on delete cascade,
  day                date   not null,
  started_at         timestamptz,
  minutes            int,
  role_key           text references ft.roles(key),
  description        text not null,
  urgent             boolean,
  important          boolean,
  displaced_item_ids bigint[] not null default '{}',
  created_at         timestamptz not null default now()
);

create table ft.checkpoints (
  id                  bigint generated always as identity primary key,
  week_id             bigint not null references ft.weeks(id) on delete cascade,
  day                 date   not null,
  kind                text   not null,
  due_at              timestamptz not null,
  focus_item_ids      bigint[] not null default '{}',
  shown_item_ids      bigint[] not null default '{}',
  sweep               boolean not null default false,
  always_send         boolean not null default false,
  preview_tomorrow    boolean not null default false,
  week_summary        boolean not null default false,
  status              text not null default 'scheduled'
                      check (status in ('scheduled', 'sent', 'answered', 'suppressed', 'expired', 'missed', 'cancelled')),
  created_by          text not null default 'rule' check (created_by in ('rule', 'claude', 'user')),
  note                text,
  sent_at             timestamptz,
  answered_at         timestamptz,
  telegram_message_id bigint,
  suppression_reason  text,
  created_at          timestamptz not null default now()
);
create index checkpoints_due on ft.checkpoints (status, due_at);

create table ft.messages (
  id                  bigint generated always as identity primary key,
  at                  timestamptz not null default now(),
  direction           text not null check (direction in ('in', 'out')),
  kind                text not null default 'text' check (kind in ('text', 'command', 'button', 'checkin', 'reply')),
  text                text,
  telegram_message_id bigint,
  checkpoint_id       bigint references ft.checkpoints(id) on delete set null
);

create table ft.day_logs (
  day                    date primary key,
  week_id                bigint not null references ft.weeks(id) on delete cascade,
  anchors                jsonb  not null,
  sleep_minutes_reported int,
  energy                 smallint check (energy between 1 and 5),
  summary                text,
  closed_at              timestamptz
);

create table ft.weekly_reviews (
  id           bigint generated always as identity primary key,
  week_id      bigint not null unique references ft.weeks(id) on delete cascade,
  stats        jsonb,
  reflections  jsonb,
  decisions    jsonb,
  next_week_id bigint references ft.weeks(id),
  created_at   timestamptz not null default now()
);

create table ft.bot_state (
  key        text primary key,
  value      jsonb not null,
  updated_at timestamptz not null default now()
);

-- Progress per weekly target. Deterministic: no now() inside, callers pass the clock.
create view ft.week_progress as
select t.week_id,
       t.activity_key,
       a.name,
       a.unit,
       t.floor,
       t.goal,
       t.provisional,
       -- A partial session counts only if it reached the activity's minimum viable length.
       coalesce(sum(case when i.status in ('done', 'partial')
                         then case when a.unit = 'minutes' then coalesce(i.actual_minutes, 0)
                                   when i.status = 'done' or coalesce(i.actual_minutes, 0) >= coalesce(a.min_minutes, 1) then 1
                                   else 0 end end), 0)::int as done,
       coalesce(sum(case when i.status in ('planned', 'unconfirmed')
                         then case when a.unit = 'minutes' then i.planned_minutes else 1 end end), 0)::int as open_amount,
       count(i.id) filter (where i.status = 'skipped')::int as skipped
from ft.weekly_targets t
join ft.activity_types a on a.key = t.activity_key
left join ft.plan_items i
       on i.week_id = t.week_id and i.activity_key = t.activity_key and i.status not in ('moved', 'cancelled')
group by t.week_id, t.activity_key, a.name, a.unit, t.floor, t.goal, t.provisional;

-- Planned versus actual minutes per role.
create view ft.role_time as
select i.week_id,
       i.role_key,
       r.name,
       r.sort,
       sum(i.planned_minutes)::int as planned_minutes,
       coalesce(sum(case when i.status in ('done', 'partial')
                         then coalesce(i.actual_minutes, i.planned_minutes) end), 0)::int as actual_minutes,
       count(*) filter (where i.status = 'skipped')::int as skipped
from ft.plan_items i
join ft.roles r on r.key = i.role_key
where i.status not in ('moved', 'cancelled')
group by i.week_id, i.role_key, r.name, r.sort;
