-- ScraperAgent SQLite schema
-- Notes:
--   * `searches` is the root of every run. Everything cascades from a search.
--   * JSON columns hold structured blobs we don't query by; promote to columns
--     only when a query needs them.
--   * Timestamps are ISO 8601 UTC strings (sqlite has no native timestamp type).

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    criteria_nl TEXT NOT NULL,
    criteria_structured_json TEXT NOT NULL,
    max_price REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | discovering | awaiting_selection | negotiating | done | failed
    thread_id TEXT,                          -- LangGraph thread id
    error_message TEXT,                      -- populated when status = 'failed' (e.g. eBay API error)
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS reference_prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    search_id INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
    source TEXT NOT NULL,                    -- amazon | walmart | bestbuy | target | google_shopping | ebay_sold
    raw_data_json TEXT NOT NULL,
    median REAL,
    p25 REAL,
    p75 REAL,
    condition TEXT,                          -- new | used
    cost_usd REAL NOT NULL DEFAULT 0,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_reference_prices_search ON reference_prices(search_id);

CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    search_id INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
    ebay_item_id TEXT NOT NULL,
    title TEXT NOT NULL,
    price REAL NOT NULL,
    shipping_cost REAL,
    condition TEXT,
    seller_id TEXT,
    seller_rating REAL,
    seller_feedback_count INTEGER,
    url TEXT NOT NULL,
    image_url TEXT,
    listed_at TEXT,                          -- when seller posted it
    raw_data_json TEXT,
    selected_at TEXT,                        -- when user selected for negotiation; NULL = not selected
    outcome TEXT,                            -- deal | walked_away | timed_out | not_selected
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_listings_search ON listings(search_id);
CREATE INDEX IF NOT EXISTS idx_listings_selected ON listings(search_id, selected_at);

CREATE TABLE IF NOT EXISTS negotiations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    strategy TEXT NOT NULL,
    strategy_inputs_json TEXT,               -- gap %, seller_rating, listing age, etc. for later analysis
    status TEXT NOT NULL DEFAULT 'open',     -- open | awaiting_seller | deal | walked_away | timed_out
    rounds INTEGER NOT NULL DEFAULT 0,
    current_offer REAL,
    final_price REAL,
    last_seller_response_at TEXT,
    walked_away_at TEXT,
    deal_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_negotiations_listing ON negotiations(listing_id);
CREATE INDEX IF NOT EXISTS idx_negotiations_status ON negotiations(status);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    negotiation_id INTEGER NOT NULL REFERENCES negotiations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,                      -- agent | seller
    body TEXT NOT NULL,
    offer_amount REAL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected | sent | received
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    approved_at TEXT,
    sent_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_negotiation ON messages(negotiation_id);
CREATE INDEX IF NOT EXISTS idx_messages_pending ON messages(status) WHERE status = 'pending';
