-- Proposed addition to kb/subaru.db (owner: kb/build_subaru_kb.py), loaded from cards.db by a new
-- builder step, e.g. `build_subaru_kb.py cards kb/cards.db`, run before `embed` and `finish`.
-- Same columns as cards.db's dtc_card, so the step is: ATTACH cards.db; DELETE FROM dtc_card;
-- INSERT INTO dtc_card SELECT * FROM c.dtc_card; INSERT INTO dtc_card_fts(dtc_card_fts) VALUES('rebuild').

CREATE TABLE IF NOT EXISTS dtc_card (
  code TEXT NOT NULL,                 -- P0303
  model TEXT NOT NULL,                -- Forester
  year_min INTEGER NOT NULL, year_max INTEGER NOT NULL,
  engine TEXT,                        -- EJ251, or NULL for every engine
  title TEXT NOT NULL,                -- 'P0303: Cylinder 3 misfire detected'
  text TEXT NOT NULL,                 -- the card as the deck shows it (plain text, source ids inline)
  card_json TEXT NOT NULL,            -- structured card: summary, causes[], fsm_steps[], caution
  sources_json TEXT NOT NULL,         -- [{id, kind, label, url}]
  forum_counts_json TEXT,             -- confirmed forum fixes per cause bucket
  dropped_json TEXT,                  -- claims removed by the source check
  llm_model TEXT, built_at TEXT,
  PRIMARY KEY (code, model, year_min, year_max)
) WITHOUT ROWID;

-- Optional: so free-text symptom questions can find a card too.
CREATE VIRTUAL TABLE IF NOT EXISTS dtc_card_fts USING fts5(
  title, text, content='dtc_card', tokenize='porter unicode61');

-- Retriever.card(code) lookup the app would use:
--   SELECT text FROM dtc_card WHERE code=? AND upper(model)=upper(?) AND ? BETWEEN year_min AND year_max
