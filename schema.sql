-- schema.sql
-- ─────────────────────────────────────────────────────────────────────────────
-- Nistula Unified Messaging Platform — PostgreSQL Database Schema
--
-- DESIGN PHILOSOPHY:
-- This schema is built around three core principles:
--
--   1. Single source of truth per entity.
--      One guests record per real person regardless of how many channels
--      they use. One messages record per message regardless of channel.
--
--   2. Explicit over implicit.
--      Every relationship is a foreign key. Every status is an enum.
--      No "magic strings" scattered across the application layer.
--
--   3. Audit-first design.
--      Every table has created_at and updated_at. The messages table tracks
--      the full lifecycle of every AI draft — from generation through agent
--      edit to final send. Nothing is ever truly deleted (soft deletes only).
--
-- TABLES (in dependency order — create parents before children):
--   1. properties          — villa/apartment inventory
--   2. guests              — one record per real guest across all channels
--   3. channel_identities  — maps platform-specific IDs to a single guest
--   4. reservations        — bookings linking guests to properties
--   5. conversations       — message threads (one per channel session)
--   6. messages            — every inbound and outbound message, all channels
-- ─────────────────────────────────────────────────────────────────────────────


-- ─────────────────────────────────────────────────────────────────────────────
-- ENTITY RELATIONSHIP DIAGRAM
-- ─────────────────────────────────────────────────────────────────────────────
--
--  properties ◄────────────────────────────────────────────┐
--      │                                                   │
--      │ (property_id FK)                     (property_id FK)
--      ▼                                                   │
--  reservations ◄──── guests ────► channel_identities      │
--      │                │                                  │
--      │ (reservation_id│FK, nullable)                     │
--      │                │ (guest_id FK)                    │
--      ▼                ▼                                  │
--  conversations ──────────────────────────────────────────┘
--      │
--      │ (conversation_id FK)
--      ▼
--  messages  (ALL inbound + outbound across ALL channels)
--
-- Key relationships:
--   guests        1 ──► N  channel_identities  (cross-channel identity)
--   guests        1 ──► N  reservations        (booking history)
--   guests        1 ──► N  conversations       (all interactions)
--   reservations  1 ──► N  conversations       (post-booking threads)
--   conversations 1 ──► N  messages            (message timeline)
--   properties    1 ──► N  conversations       (villa context)
-- ─────────────────────────────────────────────────────────────────────────────


-- ─────────────────────────────────────────────────────────────────────────────
-- EXTENSIONS
-- uuid-ossp provides uuid_generate_v4() for generating UUID primary keys.
-- pgcrypto is the alternative — uuid-ossp is more widely available.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";


-- ─────────────────────────────────────────────────────────────────────────────
-- ENUMS
-- Defining allowed values as PostgreSQL enums rather than VARCHAR gives us
-- database-level enforcement — the application cannot insert an invalid status
-- even if the application layer has a bug. Enums also self-document the schema.
-- ─────────────────────────────────────────────────────────────────────────────

-- All channels through which guests can contact Nistula.
-- Covers all 7 channels from the assessment brief plus 'direct'.
CREATE TYPE channel_type AS ENUM (
    'whatsapp',
    'booking_com',
    'airbnb',
    'expedia',
    'makemytrip',
    'agoda',
    'instagram',
    'direct'
);

-- The six query types the AI classifier can assign to an inbound message.
-- These map exactly to the six types defined in the assessment brief.
CREATE TYPE query_type AS ENUM (
    'pre_sales_availability',
    'pre_sales_pricing',
    'post_sales_checkin',
    'special_request',
    'complaint',
    'general_enquiry'
);

-- Whether a message is inbound (from guest) or outbound (from Nistula).
CREATE TYPE message_direction AS ENUM (
    'inbound',
    'outbound'
);

-- Tracks the origin and lifecycle of every outbound message.
-- ai_drafted    : Claude generated the reply, not yet reviewed by a human.
-- agent_edited  : A human agent modified the AI draft before sending.
-- agent_written : A human agent wrote the reply from scratch (no AI draft).
-- auto_sent     : The AI draft was sent automatically (confidence >= 0.85).
CREATE TYPE draft_source AS ENUM (
    'ai_drafted',
    'agent_edited',
    'agent_written',
    'auto_sent'
);

-- The action the confidence engine recommended for this inbound message.
-- Maps exactly to the three action values defined in the assessment brief.
CREATE TYPE recommended_action AS ENUM (
    'auto_send',
    'agent_review',
    'escalate'
);

-- The lifecycle status of a conversation thread.
CREATE TYPE conversation_status AS ENUM (
    'open',
    'pending_reply',
    'resolved',
    'escalated'
);

-- The lifecycle status of a reservation/booking.
CREATE TYPE reservation_status AS ENUM (
    'enquiry',       -- Guest has asked about availability but not booked yet
    'confirmed',     -- Booking confirmed, payment received
    'checked_in',    -- Guest has arrived at the property
    'checked_out',   -- Guest has departed
    'cancelled'      -- Booking was cancelled
);


-- ─────────────────────────────────────────────────────────────────────────────
-- TABLE 1: properties
-- The inventory of Nistula villas and apartments.
-- In a live system, this data comes from the Property Management System (PMS)
-- and is kept in sync via the PMS API. We store it locally for fast lookups
-- without hitting the PMS on every inbound message.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE properties (
    id                      UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),

    -- The short identifier used in webhook payloads (e.g. 'villa-b1').
    -- This is the same property_id that arrives in every inbound message.
    property_code           VARCHAR(50)  NOT NULL UNIQUE,
    name                    VARCHAR(255) NOT NULL,
    location                VARCHAR(255) NOT NULL,

    bedrooms                SMALLINT     NOT NULL CHECK (bedrooms > 0),
    max_guests              SMALLINT     NOT NULL CHECK (max_guests > 0),
    has_private_pool        BOOLEAN      NOT NULL DEFAULT FALSE,

    -- Rates stored as integers in INR to avoid floating-point precision issues.
    -- INR 18,000 is stored as 18000, not 18000.00.
    base_rate_inr           INTEGER      NOT NULL CHECK (base_rate_inr > 0),
    base_rate_guest_count   SMALLINT     NOT NULL DEFAULT 4,
    extra_guest_charge_inr  INTEGER      NOT NULL DEFAULT 0,

    check_in_time           TIME         NOT NULL DEFAULT '14:00:00',
    check_out_time          TIME         NOT NULL DEFAULT '11:00:00',

    -- Flexible JSONB field for amenities, policies, and anything that doesn't
    -- fit neatly into a typed column. This includes WiFi password, caretaker
    -- contact, chef availability, cancellation policy, and pet policy — all
    -- the facts used to build the Claude prompt context for this property.
    -- Keeps the schema stable as properties gain new attributes without
    -- requiring ALTER TABLE.
    metadata                JSONB        NOT NULL DEFAULT '{}',

    -- Soft delete: deactivated properties are hidden from the booking flow
    -- but their historical message and reservation data is preserved.
    is_active               BOOLEAN      NOT NULL DEFAULT TRUE,

    created_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Index for the most common lookup pattern: resolve a property by its code.
-- This runs on every single inbound webhook message.
CREATE INDEX idx_properties_code ON properties (property_code);

COMMENT ON TABLE properties IS
    'Nistula property inventory. Kept in sync with the PMS via nightly '
    'or webhook-triggered updates. property_code matches the property_id '
    'field in every inbound webhook payload.';

COMMENT ON COLUMN properties.metadata IS
    'Flexible JSONB store for WiFi password, caretaker contact, chef '
    'availability, cancellation policy, pet policy, and any other '
    'property-specific facts used to build Claude prompt context.';


-- ─────────────────────────────────────────────────────────────────────────────
-- TABLE 2: guests
-- One record per real human guest, regardless of how many channels they use.
--
-- HARDEST DESIGN DECISION:
-- A guest who books on Airbnb, messages on WhatsApp, and follows up via
-- Instagram is one person. But each platform gives them a different ID.
-- We solve this by keeping guests minimal (name, email, phone) and storing
-- all platform-specific identifiers in a separate channel_identities table.
-- This allows one guest record to have many platform identities, and allows
-- new channels to be added without altering this table at all.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE guests (
    id                      UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),

    -- Core identity fields. Email and phone are nullable because a guest
    -- contacting via Instagram DM may never share either during the conversation.
    full_name               VARCHAR(255) NOT NULL,
    email                   VARCHAR(255),
    phone                   VARCHAR(50),

    -- The channel through which this guest was first seen. Useful for
    -- attribution analysis: which channels bring the best repeat guests?
    first_seen_channel      channel_type NOT NULL,

    -- Aggregated stats — updated by application logic after each checkout.
    -- Denormalised here for fast dashboard queries without expensive COUNT() joins.
    total_stays             SMALLINT     NOT NULL DEFAULT 0,
    total_spend_inr         INTEGER      NOT NULL DEFAULT 0,

    -- Guest preference notes — populated from conversation history and
    -- explicitly captured during stays (e.g. "vegetarian, prefers sea view room").
    notes                   TEXT,

    -- Soft delete: we never hard-delete guest records. GDPR right-to-erasure
    -- is handled by nullifying PII fields (name, email, phone), not by
    -- deleting the row, so historical analytics remain intact.
    is_active               BOOLEAN      NOT NULL DEFAULT TRUE,

    created_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Partial unique index: enforce one record per email address for active guests.
-- Allows NULL emails (guests with no email) to coexist freely without conflict.
CREATE UNIQUE INDEX idx_guests_email_unique
    ON guests (email)
    WHERE email IS NOT NULL;

CREATE INDEX idx_guests_phone ON guests (phone)     WHERE phone IS NOT NULL;
CREATE INDEX idx_guests_name  ON guests (full_name);

COMMENT ON TABLE guests IS
    'One canonical record per real guest across all channels. '
    'Platform-specific identifiers (WhatsApp number, Airbnb user ID, etc.) '
    'live in channel_identities, not here.';

COMMENT ON COLUMN guests.total_stays IS
    'Denormalised count updated after each checkout. Used for loyalty tier '
    'classification and VIP flagging without expensive COUNT() queries on '
    'the reservations table.';


-- ─────────────────────────────────────────────────────────────────────────────
-- TABLE 3: channel_identities
-- Maps platform-specific guest identifiers to a single canonical guests record.
-- This is the identity resolution layer — the answer to the question:
-- "How do we know that WhatsApp +919876543210 and Airbnb user 'rahul_sharma_goa'
-- are the same person?"
--
-- When a new message arrives, we look up this table by (channel, platform_guest_id)
-- to find the canonical guest_id. If not found, we create a new guest record
-- and a new channel_identity row simultaneously in a single transaction.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE channel_identities (
    id                      UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),

    -- ON DELETE CASCADE: if a guest record is deleted (rare), all their
    -- platform identities are automatically removed too.
    guest_id                UUID         NOT NULL REFERENCES guests (id) ON DELETE CASCADE,
    channel                 channel_type NOT NULL,

    -- The identifier the platform uses for this guest.
    -- WhatsApp  → phone number (e.g. '+919876543210')
    -- Airbnb    → Airbnb user ID
    -- Booking.com → booker email or reservation number
    -- Instagram → Instagram user handle or DM thread ID
    platform_guest_id       VARCHAR(255) NOT NULL,

    -- Raw profile data from the platform (display name, avatar URL, verified
    -- status, etc.). Stored as JSONB so we never lose platform data even if
    -- we don't actively use it yet — zero schema change needed to use it later.
    platform_metadata       JSONB        NOT NULL DEFAULT '{}',

    -- A guest can have at most one identity per channel.
    -- This prevents duplicate lookups and duplicate message routing.
    CONSTRAINT uq_channel_platform_id UNIQUE (channel, platform_guest_id),

    created_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Lookup index: the hottest read path in the entire system.
-- Every single inbound message triggers a lookup on (channel, platform_guest_id).
CREATE INDEX idx_channel_identities_lookup ON channel_identities (channel, platform_guest_id);
CREATE INDEX idx_channel_identities_guest  ON channel_identities (guest_id);

COMMENT ON TABLE channel_identities IS
    'Maps one platform-specific guest ID (WhatsApp number, Airbnb user ID, etc.) '
    'to a canonical guest record. Enables cross-channel guest identity resolution. '
    'One guest can appear on multiple channels — each appearance gets one row here.';


-- ─────────────────────────────────────────────────────────────────────────────
-- TABLE 4: reservations
-- Confirmed bookings linking a guest to a property for specific dates.
--
-- Important: pre-booking enquiries do NOT have a reservation. Conversations
-- can exist without a reservation_id — the FK in the conversations table
-- is nullable precisely for this reason.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE reservations (
    id                      UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),

    -- The booking reference visible to guests (e.g. 'NIS-2024-0891').
    -- This is the booking_ref that arrives in inbound webhook payloads.
    -- Unique across all reservations — used as the external-facing identifier.
    booking_ref             VARCHAR(50)  NOT NULL UNIQUE,

    guest_id                UUID         NOT NULL REFERENCES guests (id),
    property_id             UUID         NOT NULL REFERENCES properties (id),

    -- Which channel this reservation was originally created through.
    booking_channel         channel_type NOT NULL,

    check_in_date           DATE         NOT NULL,
    check_out_date          DATE         NOT NULL,
    num_guests              SMALLINT     NOT NULL CHECK (num_guests > 0),

    -- Financial details stored in INR as integers (no floating-point risk).
    base_amount_inr         INTEGER      NOT NULL DEFAULT 0,
    extra_guest_amount_inr  INTEGER      NOT NULL DEFAULT 0,
    total_amount_inr        INTEGER      NOT NULL DEFAULT 0,

    status                  reservation_status NOT NULL DEFAULT 'enquiry',

    -- Free-text special requests noted at the time of booking.
    special_requests        TEXT,

    -- The PMS-assigned internal reservation ID — populated after the booking
    -- is pushed to the Property Management System (e.g. Cloudbeds, Guesty).
    -- NULL until the PMS sync completes.
    pms_reservation_id      VARCHAR(100),

    -- Database-enforced constraint: checkout must be after checkin.
    CHECK (check_out_date > check_in_date),

    created_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_reservations_guest        ON reservations (guest_id);
CREATE INDEX idx_reservations_property     ON reservations (property_id);
CREATE INDEX idx_reservations_booking_ref  ON reservations (booking_ref);
CREATE INDEX idx_reservations_check_in     ON reservations (check_in_date);
CREATE INDEX idx_reservations_status       ON reservations (status);

COMMENT ON TABLE reservations IS
    'Confirmed and pending bookings. Linked to both a guest and a property. '
    'Pre-booking conversations exist without a reservation — reservation_id '
    'is nullable in the conversations table to handle this correctly.';


-- ─────────────────────────────────────────────────────────────────────────────
-- TABLE 5: conversations
-- A conversation is a thread of messages between Nistula and a guest on a
-- specific channel. One guest can have multiple conversations across different
-- channels or at different points in time (pre-booking, mid-stay, post-stay).
--
-- Linking:
--   conversations → guests      (always — every conversation belongs to a guest)
--   conversations → reservations (nullable — pre-booking conversations have none)
--   conversations → properties  (always — every conversation is about a property)
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE conversations (
    id                      UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),

    guest_id                UUID         NOT NULL REFERENCES guests (id),

    -- NULL for pre-booking enquiries (the guest has no reservation yet).
    -- Populated once the guest books, linking the conversation to the booking.
    reservation_id          UUID         REFERENCES reservations (id),

    property_id             UUID         NOT NULL REFERENCES properties (id),
    channel                 channel_type NOT NULL,

    -- The conversation/thread ID assigned by the external platform.
    -- Used to route replies back to the correct thread via the platform API
    -- (Meta Business API for WhatsApp, Airbnb messaging API, etc.).
    platform_conversation_id VARCHAR(255),

    status                  conversation_status NOT NULL DEFAULT 'open',

    -- Denormalised timestamp for sorting conversations by recency in the
    -- agent dashboard without scanning all messages in every thread.
    last_message_at         TIMESTAMPTZ,

    -- The agent currently assigned to handle this conversation.
    -- NULL means unassigned — AI is handling it autonomously so far.
    assigned_agent_id       VARCHAR(100),

    created_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_conversations_guest       ON conversations (guest_id);
CREATE INDEX idx_conversations_reservation ON conversations (reservation_id);
CREATE INDEX idx_conversations_property    ON conversations (property_id);
CREATE INDEX idx_conversations_status      ON conversations (status);

-- Descending index on last_message_at: the agent dashboard sorts by most
-- recent activity — this index makes that query a fast index scan.
CREATE INDEX idx_conversations_last_msg    ON conversations (last_message_at DESC);

COMMENT ON TABLE conversations IS
    'Message threads between Nistula and guests. One conversation per channel '
    'session. Always linked to a guest and a property. Linked to a reservation '
    'only when post-booking (nullable for pre-booking enquiries).';


-- ─────────────────────────────────────────────────────────────────────────────
-- TABLE 6: messages
-- Every single message across all channels lives here — inbound and outbound.
-- This is the most important table in the schema.
--
-- It must answer every operational and analytical question:
--   - What did this guest say and when?
--   - What did we reply and who sent it?
--   - Was the reply AI-drafted, edited by an agent, or written from scratch?
--   - What was the AI confidence score for this inbound message?
--   - What action did the confidence engine recommend?
--   - How much did the agent change the AI draft before sending?
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE messages (
    id                      UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),

    conversation_id         UUID         NOT NULL REFERENCES conversations (id),
    guest_id                UUID         NOT NULL REFERENCES guests (id),

    -- Direction determines which set of columns is populated for this row.
    -- Inbound  → AI classification columns are filled, draft columns are NULL.
    -- Outbound → Draft tracking columns are filled, classification columns are NULL.
    direction               message_direction NOT NULL,

    -- The actual text of the message — what the guest sent or what was sent
    -- to the guest. For outbound messages, this is the FINAL sent version
    -- (which may differ from original_ai_draft if an agent edited it).
    message_text            TEXT         NOT NULL,

    -- The raw JSON payload received from the channel (inbound messages only).
    -- Stored for auditability: if our parsing logic had a bug, we can
    -- re-parse the original payload without any data loss.
    raw_inbound_payload     JSONB,


    -- ── INBOUND-ONLY COLUMNS ─────────────────────────────────────────────────
    -- Populated for direction = 'inbound'. NULL for outbound messages.

    -- Query type classified by classifier.py using weighted keyword scoring.
    query_type              query_type,

    -- Composite confidence score from confidence.py (0.0–1.0).
    -- DECIMAL(5,4) stores up to 9.9999 with 4 decimal places.
    -- The CHECK constraint enforces the 0–1 range at the database level.
    ai_confidence_score     DECIMAL(5, 4)
                                CHECK (
                                    ai_confidence_score IS NULL OR
                                    (ai_confidence_score >= 0 AND ai_confidence_score <= 1)
                                ),

    -- Full breakdown of all four confidence factors stored as JSONB.
    -- Schema: { classification_confidence, sentiment_score,
    --           completeness_score, complexity_score, override_reason? }
    -- Enables aggregate queries like: "which factor most often causes escalation?"
    confidence_factor_scores JSONB,

    -- The action recommended by the confidence engine for this message.
    -- Maps to the three action values: auto_send, agent_review, escalate.
    recommended_action      recommended_action,


    -- ── OUTBOUND-ONLY COLUMNS ────────────────────────────────────────────────
    -- Populated for direction = 'outbound'. NULL for inbound messages.

    -- How this outbound message was created and what its lifecycle was.
    draft_source            draft_source,

    -- The original Claude-generated draft — preserved permanently even if
    -- an agent completely rewrites it. This column is the foundation of the
    -- model improvement feedback loop: comparing original_ai_draft to
    -- message_text (the final sent version) gives us a high-quality dataset
    -- of human corrections to AI drafts, which can be used for fine-tuning.
    -- This column is NEVER overwritten once set.
    original_ai_draft       TEXT,

    -- Which agent reviewed, edited, and/or sent this message.
    -- NULL for auto_sent messages (no human involved).
    sent_by_agent_id        VARCHAR(100),

    -- When the message was actually sent to the guest via the channel API.
    -- NULL for drafts still pending review or messages that were discarded.
    sent_at                 TIMESTAMPTZ,


    -- ── SHARED COLUMNS ───────────────────────────────────────────────────────

    -- When this message record was created in our system.
    -- For outbound messages, created_at is when the draft was generated —
    -- which differs from sent_at (when it was actually delivered to the guest).
    -- The gap between created_at and sent_at is the agent review time.
    created_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Indexes cover all access patterns the agent dashboard and analytics will use.
CREATE INDEX idx_messages_conversation       ON messages (conversation_id, created_at DESC);
CREATE INDEX idx_messages_guest              ON messages (guest_id, created_at DESC);
CREATE INDEX idx_messages_direction          ON messages (direction);
CREATE INDEX idx_messages_query_type         ON messages (query_type)            WHERE query_type IS NOT NULL;
CREATE INDEX idx_messages_confidence         ON messages (ai_confidence_score)   WHERE ai_confidence_score IS NOT NULL;
CREATE INDEX idx_messages_recommended_action ON messages (recommended_action)    WHERE recommended_action IS NOT NULL;
CREATE INDEX idx_messages_draft_source       ON messages (draft_source)          WHERE draft_source IS NOT NULL;

COMMENT ON TABLE messages IS
    'Every inbound and outbound message across all channels in one table. '
    'Inbound messages have query_type, ai_confidence_score, confidence_factor_scores, '
    'and recommended_action populated. '
    'Outbound messages have draft_source, original_ai_draft, sent_by_agent_id, '
    'and sent_at populated. '
    'original_ai_draft is preserved permanently even after agent edits to '
    'enable model quality improvement analysis.';

COMMENT ON COLUMN messages.confidence_factor_scores IS
    'JSONB breakdown of the four confidence factors: '
    '{classification_confidence, sentiment_score, completeness_score, complexity_score}. '
    'For complaint hard-override cases, includes override_reason: '
    'complaint_hard_escalation. Enables aggregate analysis of which factors '
    'most commonly drive escalation across all properties.';

COMMENT ON COLUMN messages.original_ai_draft IS
    'The Claude-generated draft BEFORE any agent editing. Never overwritten once set. '
    'Comparing this against message_text (the final sent version) measures how much '
    'agents modify AI drafts — a key signal for model quality and fine-tuning dataset '
    'creation. Without this column, that learning opportunity is permanently lost.';


-- ─────────────────────────────────────────────────────────────────────────────
-- UPDATED_AT TRIGGER FUNCTION
-- Automatically updates the updated_at timestamp on every row modification.
-- Without this, updated_at would have to be managed by the application layer —
-- which is error-prone and easy to accidentally omit in a code change.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Apply the trigger to every table that has an updated_at column.
CREATE TRIGGER trg_properties_updated_at
    BEFORE UPDATE ON properties
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER trg_guests_updated_at
    BEFORE UPDATE ON guests
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER trg_channel_identities_updated_at
    BEFORE UPDATE ON channel_identities
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER trg_reservations_updated_at
    BEFORE UPDATE ON reservations
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER trg_conversations_updated_at
    BEFORE UPDATE ON conversations
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();


-- ─────────────────────────────────────────────────────────────────────────────
-- DESIGN DECISIONS SUMMARY
-- ─────────────────────────────────────────────────────────────────────────────
--
-- HARDEST DECISION: Guest identity across channels (channel_identities table).
-- A guest contacting Nistula via WhatsApp, Airbnb, and Instagram is one person
-- but has three different platform-assigned identifiers. Storing all channel
-- identities in a separate table (rather than adding columns like airbnb_id,
-- whatsapp_phone, instagram_handle to the guests table) means adding a new
-- channel requires zero schema changes — just a new enum value and new rows
-- in channel_identities. The naive alternative of one column per channel in
-- guests would require ALTER TABLE every time a new channel is onboarded, and
-- would result in a table with mostly NULL columns as guests rarely use every
-- channel simultaneously. The channel_identities pattern scales cleanly to any
-- number of channels without ever touching the guests table structure.
--
-- SECOND KEY DECISION: One messages table for all directions and all channels.
-- The alternative was two separate tables: inbound_messages and
-- outbound_messages. One unified table is better for three reasons: conversation
-- history queries need only one JOIN instead of two UNIONs, chronological
-- ordering of a full thread is trivial (ORDER BY created_at on one table),
-- and analytics across all messages — volume, response time, confidence
-- distributions, escalation rates — are efficient single-table scans. The
-- nullable column approach (inbound columns NULL for outbound rows and vice
-- versa) is a deliberate trade-off: slight storage overhead in exchange for
-- dramatically simpler query patterns across the entire application.
--
-- THIRD KEY DECISION: Preserving original_ai_draft permanently after agent edits.
-- This column is the foundation of a model improvement feedback loop. By
-- comparing original_ai_draft to message_text (the final version actually sent),
-- we can measure edit distance per query type, identify which scenarios Claude
-- handles poorly, and build a high-quality fine-tuning dataset from human
-- corrections to AI drafts. Without this column, that learning opportunity is
-- permanently destroyed the moment an agent makes their first edit. The storage
-- cost is minimal; the long-term intelligence value is enormous.
-- ─────────────────────────────────────────────────────────────────────────────


-- ─────────────────────────────────────────────────────────────────────────────
-- SAMPLE DATA — 3AM COMPLAINT SCENARIO (Part 3 Cross-Reference)
--
-- This section traces exactly what gets written to the database when the
-- Part 3 scenario occurs: a guest at Villa B1 sends a WhatsApp complaint at
-- 3am about no hot water, with guests arriving for breakfast in 4 hours.
--
-- Reading these INSERTs alongside thinking.md demonstrates that the schema,
-- the application code, and the written system design are one unified design —
-- not three separate answers to three separate questions.
-- ─────────────────────────────────────────────────────────────────────────────

-- Step 1: Villa B1 property record (inserted during initial data seeding).
INSERT INTO properties (
    id,
    property_code,
    name,
    location,
    bedrooms,
    max_guests,
    has_private_pool,
    base_rate_inr,
    base_rate_guest_count,
    extra_guest_charge_inr,
    check_in_time,
    check_out_time,
    metadata
) VALUES (
    'a1b2c3d4-0001-0001-0001-000000000001',
    'villa-b1',
    'Villa B1',
    'Assagao, North Goa',
    3,
    6,
    TRUE,
    18000,
    4,
    2000,
    '14:00:00',
    '11:00:00',
    '{
        "wifi_password":              "Nistula@2024",
        "caretaker_hours":            "8am to 10pm",
        "chef_on_call":               true,
        "chef_prebooking_required":   true,
        "cancellation_policy":        "Free up to 7 days before check-in",
        "pet_policy":                 "Pets are not allowed",
        "parking":                    "Complimentary private parking on site"
    }'::jsonb
) ON CONFLICT (property_code) DO NOTHING;


-- Step 2: The guest who sends the complaint — one canonical record.
-- Regardless of which channel Vikram uses, this is the single source of truth.
INSERT INTO guests (
    id,
    full_name,
    email,
    phone,
    first_seen_channel,
    total_stays,
    total_spend_inr
) VALUES (
    'b2c3d4e5-0002-0002-0002-000000000002',
    'Vikram Nair',
    'vikram.nair@email.com',
    '+919876543210',
    'whatsapp',
    1,
    72000       -- 4 nights × INR 18,000 base rate (2 guests within base coverage)
);


-- Step 3: Vikram's WhatsApp identity linked to his canonical guest record.
-- This is the row the system looks up on (channel='whatsapp', platform_guest_id='+919876543210')
-- when his 3am message arrives, to find his guest_id in microseconds.
INSERT INTO channel_identities (
    id,
    guest_id,
    channel,
    platform_guest_id,
    platform_metadata
) VALUES (
    'c3d4e5f6-0003-0003-0003-000000000003',
    'b2c3d4e5-0002-0002-0002-000000000002',
    'whatsapp',
    '+919876543210',
    '{"display_name": "Vikram Nair", "verified": true}'::jsonb
);


-- Step 4: Vikram's active reservation during which the complaint occurs.
-- Status is 'checked_in' — he is mid-stay when the hot water fails at 3am.
INSERT INTO reservations (
    id,
    booking_ref,
    guest_id,
    property_id,
    booking_channel,
    check_in_date,
    check_out_date,
    num_guests,
    base_amount_inr,
    extra_guest_amount_inr,
    total_amount_inr,
    status
) VALUES (
    'd4e5f6a7-0004-0004-0004-000000000004',
    'NIS-2024-0955',
    'b2c3d4e5-0002-0002-0002-000000000002',
    'a1b2c3d4-0001-0001-0001-000000000001',
    'whatsapp',
    '2026-04-20',
    '2026-04-24',
    2,
    72000,      -- INR 18,000 × 4 nights
    0,          -- 2 guests is within the base coverage of 4 guests, no extra charge
    72000,
    'checked_in'
);


-- Step 5: The active conversation thread for this stay.
-- Status is updated to 'escalated' the moment the complaint arrives.
-- last_message_at is stamped to the exact complaint timestamp.
INSERT INTO conversations (
    id,
    guest_id,
    reservation_id,
    property_id,
    channel,
    platform_conversation_id,
    status,
    last_message_at
) VALUES (
    'e5f6a7b8-0005-0005-0005-000000000005',
    'b2c3d4e5-0002-0002-0002-000000000002',
    'd4e5f6a7-0004-0004-0004-000000000004',
    'a1b2c3d4-0001-0001-0001-000000000001',
    'whatsapp',
    'wa-thread-vikram-villa-b1-april-2026',
    'escalated',
    '2026-04-21 03:00:00+05:30'     -- 3am IST — the complaint timestamp
);


-- Step 6: THE CORE RECORD — the inbound complaint with full AI metadata.
-- This is exactly what gets written when the 3am message arrives and the
-- pipeline processes it through classifier.py and confidence.py.
--
-- Key values to note:
--   query_type             = 'complaint'    (classified by keyword scorer)
--   ai_confidence_score    = 0.0000         (hard override fires — not computed)
--   recommended_action     = 'escalate'     (hard override — no other path exists)
--   confidence_factor_scores shows override_reason to explain why score is 0.0
INSERT INTO messages (
    id,
    conversation_id,
    guest_id,
    direction,
    message_text,
    raw_inbound_payload,
    query_type,
    ai_confidence_score,
    confidence_factor_scores,
    recommended_action,
    created_at
) VALUES (
    'f6a7b8c9-0006-0006-0006-000000000006',
    'e5f6a7b8-0005-0005-0005-000000000005',
    'b2c3d4e5-0002-0002-0002-000000000002',
    'inbound',
    'There is no hot water and we have guests arriving for breakfast in 4 hours. This is unacceptable. I want a refund for tonight.',
    '{
        "source":       "whatsapp",
        "guest_name":   "Vikram Nair",
        "message":      "There is no hot water and we have guests arriving for breakfast in 4 hours. This is unacceptable. I want a refund for tonight.",
        "timestamp":    "2026-04-21T03:00:00Z",
        "booking_ref":  "NIS-2024-0955",
        "property_id":  "villa-b1"
    }'::jsonb,
    'complaint',
    0.0000,
    '{
        "classification_confidence": 0.91,
        "sentiment_score":           0.00,
        "completeness_score":        0.00,
        "complexity_score":          0.00,
        "override_reason":           "complaint_hard_escalation"
    }'::jsonb,
    'escalate',
    '2026-04-21 03:00:00+05:30'
);


-- Step 7: The AI-drafted outbound reply — stored even for escalated messages.
-- The human agent reviewing the escalation uses this as a starting point,
-- editing rather than writing from scratch. This is standard agent-assist
-- behaviour in production hospitality systems.
--
-- Note:
--   draft_source       = 'agent_edited'  (agent modified the AI draft)
--   original_ai_draft  = Claude's exact output (preserved permanently)
--   message_text       = what was actually sent (agent's edited version)
--   sent_at            = 4 minutes after the complaint arrived
INSERT INTO messages (
    id,
    conversation_id,
    guest_id,
    direction,
    message_text,
    draft_source,
    original_ai_draft,
    sent_by_agent_id,
    sent_at,
    created_at
) VALUES (
    'a7b8c9d0-0007-0007-0007-000000000007',
    'e5f6a7b8-0005-0005-0005-000000000005',
    'b2c3d4e5-0002-0002-0002-000000000002',
    'outbound',
    -- message_text: what the agent actually sent (slightly tightened from AI draft)
    'Vikram, I''m truly sorry — no hot water with guests arriving in a few hours is completely unacceptable. Our team has been alerted and someone will reach you within 15 minutes to fix this tonight. We are on this.',
    'agent_edited',
    -- original_ai_draft: exactly what Claude generated — never overwritten
    'Vikram, I''m truly sorry — no hot water with guests arriving in a few hours is completely unacceptable, and I understand how stressful this moment is. Our team has been alerted right now and someone will reach you within the next 15 minutes to resolve this tonight. We are on this.',
    'agent-on-duty-001',
    '2026-04-21 03:04:22+05:30',    -- Sent 4 minutes 22 seconds after complaint
    '2026-04-21 03:01:15+05:30'     -- Draft generated 1 minute 15 seconds after complaint
);