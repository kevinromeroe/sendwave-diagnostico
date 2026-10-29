-- =============================================================
-- Esquema Supabase — Dashboard de redes (multi-cliente)
-- Ejecutar UNA vez en el SQL Editor del proyecto nuevo de SendWave.
--
-- Diferencia con el proyecto de Comapan: `periods` tiene PK compuesta
-- (client_id, id), así dos clientes pueden tener su propio '2026-11'.
-- =============================================================

create table if not exists clients (
  id           text primary key,
  name         text not null,
  agency_name  text,
  created_at   timestamptz not null default now()
);

create table if not exists periods (
  client_id    text not null references clients(id) on delete cascade,
  id           text not null,                -- 'diagnostico' | 'YYYY-MM'
  label        text not null,
  starts_on    date not null,
  ends_on      date not null,
  is_baseline  boolean not null default false,
  primary key (client_id, id)
);

-- Snapshot del perfil de cada cuenta por período.
-- `platform` guarda la clave del canal del YAML (ej. ig_latam, ig_brasil).
create table if not exists accounts (
  client_id     text not null,
  period_id     text not null,
  platform      text not null,
  username      text,
  display_name  text,
  bio           text,
  followers     integer,
  following_n   integer,
  posts_total   integer,
  verified      boolean,
  is_business   boolean,
  category      text,
  website       text,
  external_url  text,
  page_likes    integer,
  views_window  integer,
  rating        text,
  raw           jsonb,
  snapshot_at   timestamptz,
  primary key (client_id, period_id, platform),
  foreign key (client_id, period_id) references periods(client_id, id) on delete cascade
);

-- Publicaciones. No llevan period_id: el período se asigna por posted_at.
create table if not exists posts (
  id                       text primary key,   -- id nativo de la red
  client_id                text not null references clients(id) on delete cascade,
  platform                 text not null,      -- clave del canal (ig_latam, ig_brasil, …)
  url                      text not null default '',
  type                     text,               -- Image | Sidecar | Video | …
  caption                  text,
  hashtags                 text[] not null default '{}',
  posted_at                timestamptz not null,
  likes                    integer not null default 0,
  comments                 integer not null default 0,
  shares                   integer not null default 0,
  views                    integer,            -- reproducciones de reels (no suman engagement)
  engagement               integer not null default 0,
  media_url                text,               -- URL del CDN (expira en horas)
  media_url_local          text,               -- /assets/thumbs/xxx.jpg (en el repo)
  thumbnail_downloaded_at  timestamptz,
  tags                     jsonb,              -- ["promocion","tutorial"]
  tag_primary              text,
  tagged_at                timestamptz,
  is_ad                    boolean not null default false,
  is_pinned                boolean not null default false,
  raw                      jsonb
);

create index if not exists posts_client_posted_idx on posts (client_id, posted_at);
create index if not exists posts_client_platform_idx on posts (client_id, platform);

-- Seguridad: RLS activo y SIN políticas → solo la service_role key (Actions) lee y escribe.
-- La anon key no puede leer nada.
alter table clients  enable row level security;
alter table periods  enable row level security;
alter table accounts enable row level security;
alter table posts    enable row level security;

-- Historial de seguidores: una medición por cuenta y día (captura semanal +
-- cada ingesta). Instagram no expone el histórico, así que la serie se arma aquí.
create table if not exists followers_history (
  client_id    text not null references clients(id) on delete cascade,
  platform     text not null,                -- clave del canal (ig_latam, ig_brasil, …)
  captured_on  date not null,                -- día de la medición (hora Bogotá)
  followers    integer not null,
  following_n  integer,
  posts_total  integer,
  captured_at  timestamptz not null default now(),
  primary key (client_id, platform, captured_on)
);
alter table followers_history enable row level security;

-- Semilla del cliente (la ingesta también la crea si falta)
insert into clients (id, name) values ('sendwave', 'SendWave')
on conflict (id) do nothing;

insert into periods (client_id, id, label, starts_on, ends_on, is_baseline)
values ('sendwave', 'diagnostico', 'Diagnóstico (Jun-Oct 2026)', '2026-06-01', '2026-10-31', true)
on conflict (client_id, id) do nothing;
