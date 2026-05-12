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
-- EXTENSIONS
-- uuid-ossp provides gen_random_uuid() for generating UUID primary keys.
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
-- ai_drafted    : Claude generated the reply, not yet reviewed.
-- agent_edited  : A human agent modified the AI draft before sending.
-- agent_written : A human agent wrote the reply from scratch (no AI draft).
-- auto_sent     : The AI draft was sent automatically (confidence >= 0.85).
CREATE TYPE draft_source AS ENUM (
    'ai_drafted',
    'agent_edited',
    'agent_written',
    'auto_sent'
);

-- The action the confidence engine recommended for this message.
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
    'enquiry',       -- Guest has asked about availability but not booked
    'confirmed',     -- Booking confirmed, payment received
    'checked_in',    -- Guest has arrived
    'checked_out',   -- Guest has departed
    'cancelled'      -- Booking cancelled
);


-- ─────────────────────────────────────────────────────────────────────────────
-- TABLE 1: properties
-- The inventory of Nistula villas and apartments.
-- In a live system, this data comes from the Property Management System (PMS)
-- and is kept in sync via the PMS API. We store it locally for fast lookups
-- without hitting the PMS on every message.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE properties (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    -- The short identifier used in webhook payloads (e.g. 'villa-b1').
    property_code           VARCHAR(50)  NOT NULL UNIQUE,
    name                    VARCHAR(255) NOT NULL,
    location                VARCHAR(255) NOT NULL,

    bedrooms                SMALLINT     NOT NULL CHECK (bedrooms > 0),
    max_guests              SMALLINT     NOT NULL CHECK (max_guests > 0),
    has_private_pool        BOOLEAN      NOT NULL DEFAULT FALSE,

    -- Rates stored as integers (paise or rupees) to avoid floating-point issues.
    -- We store in INR (rupees) as integers for simplicity.
    base_rate_inr           INTEGER      NOT NULL CHECK (base_rate_inr > 0),
    base_rate_guest_count   SMALLINT     NOT NULL DEFAULT 4,
    extra_guest_charge_inr  INTEGER      NOT NULL DEFAULT 0,

    check_in_time           TIME         NOT NULL DEFAULT '14:00:00',
    check_out_time          TIME         NOT NULL DEFAULT '11:00:00',

    -- Flexible JSON field for amenities, policies, and anything that doesn't
    -- fit neatly into a typed column. Keeps the schema stable as properties
    -- gain new attributes without requiring ALTER TABLE.
    metadata                JSONB        NOT NULL DEFAULT '{}',

    is_active               BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Index for the most common lookup pattern: find a property by its code.
CREATE INDEX idx_properties_code ON properties (property_code);

COMMENT ON TABLE properties IS
    'Nistula property inventory. Kept in sync with the PMS via nightly or webhook-triggered updates.';

COMMENT ON COLUMN properties.metadata IS
    'Flexible JSONB store for WiFi password, caretaker contact, chef availability, '
    'cancellation policy, pet policy, and any other property-specific facts used '
    'to build Claude prompt context.';


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
-- new channels to be added without altering this table.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE guests (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    -- Core identity fields. Email and phone are nullable because a guest
    -- contacting via Instagram DM may never provide either.
    full_name               VARCHAR(255) NOT NULL,
    email                   VARCHAR(255),
    phone                   VARCHAR(50),

    -- The channel through which this guest was first seen. Useful for
    -- attribution analysis (which channels bring the best repeat guests?).
    first_seen_channel      channel_type NOT NULL,

    -- Aggregated stats — updated by application logic after each stay.
    -- Denormalised here for fast dashboard queries without expensive joins.
    total_stays             SMALLINT     NOT NULL DEFAULT 0,
    total_spend_inr         INTEGER      NOT NULL DEFAULT 0,

    -- Guest preference notes — populated from conversation history and
    -- explicitly captured during stays (e.g. "vegetarian, prefers sea view").
    notes                   TEXT,

    -- Soft delete: we never hard-delete guest records (GDPR right-to-erasure
    -- is handled by nullifying PII fields, not deleting the row).
    is_active               BOOLEAN      NOT NULL DEFAULT TRUE,

    created_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Partial unique index: enforce unique email per active guest.
-- Allows NULL emails (guests without email) to coexist freely.
CREATE UNIQUE INDEX idx_guests_email_unique
    ON guests (email)
    WHERE email IS NOT NULL;

CREATE INDEX idx_guests_phone ON guests (phone) WHERE phone IS NOT NULL;
CREATE INDEX idx_guests_name  ON guests (full_name);

COMMENT ON TABLE guests IS
    'One record per real guest across all channels. '
    'Platform-specific identifiers live in channel_identities.';

COMMENT ON COLUMN guests.total_stays IS
    'Denormalised count updated after each checkout. '
    'Used for loyalty tier classification without expensive COUNT() queries.';


-- ─────────────────────────────────────────────────────────────────────────────
-- TABLE 3: channel_identities
-- Maps platform-specific guest identifiers to a single guests record.
-- This is the identity resolution layer — the answer to "how do we know
-- that WhatsApp +919876543210 and Airbnb user 'rahul_sharma_goa' are the
-- same person?"
--
-- When a new message arrives from a known platform ID, we look up this table
-- to find the canonical guest_id. If not found, we create a new guest record
-- and a new channel_identity row.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE channel_identities (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    guest_id                UUID         NOT NULL REFERENCES guests (id) ON DELETE CASCADE,
    channel                 channel_type NOT NULL,

    -- The identifier the platform uses for this guest.
    -- For WhatsApp: phone number. For Airbnb: Airbnb user ID.
    -- For Booking.com: booker email or reservation number. Etc.
    platform_guest_id       VARCHAR(255) NOT NULL,

    -- Raw profile data from the platform (display name, avatar URL, etc.)
    -- Stored as JSONB so we never lose platform data even if we don't use it yet.
    platform_metadata       JSONB        NOT NULL DEFAULT '{}',

    -- One guest can have at most one identity per channel.
    CONSTRAINT uq_channel_platform_id UNIQUE (channel, platform_guest_id),

    created_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_channel_identities_guest    ON channel_identities (guest_id);
CREATE INDEX idx_channel_identities_lookup   ON channel_identities (channel, platform_guest_id);

COMMENT ON TABLE channel_identities IS
    'Maps one platform-specific guest ID (WhatsApp number, Airbnb user ID, etc.) '
    'to a canonical guest record. Enables cross-channel guest identity resolution. '
    'One guest can appear on multiple channels — each gets one row here.';


-- ─────────────────────────────────────────────────────────────────────────────
-- TABLE 4: reservations
-- Confirmed bookings linking a guest to a property for specific dates.
-- Pre-booking enquiries do NOT have a reservation — conversations can exist
-- without a reservation_id (nullable FK in conversations table).
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE reservations (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    -- The booking reference visible to guests (e.g. 'NIS-2024-0891').
    -- Unique across all reservations and used as the external-facing identifier.
    booking_ref             VARCHAR(50)  NOT NULL UNIQUE,

    guest_id                UUID         NOT NULL REFERENCES guests (id),
    property_id             UUID         NOT NULL REFERENCES properties (id),

    -- Booking source — which channel this reservation came through.
    booking_channel         channel_type NOT NULL,

    check_in_date           DATE         NOT NULL,
    check_out_date          DATE         NOT NULL,
    num_guests              SMALLINT     NOT NULL CHECK (num_guests > 0),

    -- Financial details stored in INR as integers.
    base_amount_inr         INTEGER      NOT NULL DEFAULT 0,
    extra_guest_amount_inr  INTEGER      NOT NULL DEFAULT 0,
    total_amount_inr        INTEGER      NOT NULL DEFAULT 0,

    status                  reservation_status NOT NULL DEFAULT 'enquiry',

    -- Special requests noted at time of booking.
    special_requests        TEXT,

    -- The PMS-assigned reservation ID — populated after syncing with the
    -- Property Management System (e.g. Cloudbeds, Guesty).
    pms_reservation_id      VARCHAR(100),

    CHECK (check_out_date > check_in_date),

    created_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_reservations_guest          ON reservations (guest_id);
CREATE INDEX idx_reservations_property       ON reservations (property_id);
CREATE INDEX idx_reservations_booking_ref    ON reservations (booking_ref);
CREATE INDEX idx_reservations_check_in       ON reservations (check_in_date);
CREATE INDEX idx_reservations_status         ON reservations (status);

COMMENT ON TABLE reservations IS
    'Confirmed and pending bookings. Linked to both a guest and a property. '
    'Pre-booking conversations exist without a reservation (reservation_id is nullable in conversations).';


-- ─────────────────────────────────────────────────────────────────────────────
-- TABLE 5: conversations
-- A conversation is a thread of messages between Nistula and a guest on a
-- specific channel. One guest can have multiple conversations across different
-- channels or at different points in time.
--
-- Linking: conversations → guests (always) + reservations (when post-booking).
-- Pre-booking conversations have reservation_id = NULL.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE conversations (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    guest_id                UUID         NOT NULL REFERENCES guests (id),

    -- NULL for pre-booking enquiries (guest has no reservation yet).
    reservation_id          UUID         REFERENCES reservations (id),

    property_id             UUID         NOT NULL REFERENCES properties (id),
    channel                 channel_type NOT NULL,

    -- The conversation/thread ID assigned by the external platform.
    -- Used to route replies back to the correct thread via the platform API.
    platform_conversation_id VARCHAR(255),

    status                  conversation_status NOT NULL DEFAULT 'open',

    -- Denormalised timestamp for sorting conversations by recency without
    -- scanning all messages in the thread.
    last_message_at         TIMESTAMPTZ,

    -- The agent currently assigned to handle this conversation.
    -- NULL means unassigned (AI-only so far, or not yet picked up).
    assigned_agent_id       VARCHAR(100),

    created_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_conversations_guest         ON conversations (guest_id);
CREATE INDEX idx_conversations_reservation   ON conversations (reservation_id);
CREATE INDEX idx_conversations_property      ON conversations (property_id);
CREATE INDEX idx_conversations_status        ON conversations (status);
CREATE INDEX idx_conversations_last_msg      ON conversations (last_message_at DESC);

COMMENT ON TABLE conversations IS
    'Message threads between Nistula and guests. '
    'One conversation per channel session. Linked to a guest always, '
    'to a reservation when post-booking (nullable otherwise).';


-- ─────────────────────────────────────────────────────────────────────────────
-- TABLE 6: messages
-- Every single message across all channels lives here — inbound and outbound.
-- This is the most important table in the schema. It must answer questions like:
--   - What did this guest say and when?
--   - What did we reply and who sent it?
--   - Was the reply AI-drafted, edited by an agent, or written from scratch?
--   - What was the AI confidence score for this inbound message?
--   - What action did the confidence engine recommend?
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE messages (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    conversation_id         UUID         NOT NULL REFERENCES conversations (id),
    guest_id                UUID         NOT NULL REFERENCES guests (id),

    -- Direction determines which columns are populated.
    -- Inbound: AI classification columns are filled.
    -- Outbound: Draft tracking columns are filled.
    direction               message_direction NOT NULL,

    message_text            TEXT         NOT NULL,

    -- The raw payload received from the channel (for inbound messages).
    -- Stored for auditability — if our parsing logic had a bug, we can
    -- re-parse from the original raw payload without data loss.
    raw_inbound_payload     JSONB,

    -- ── INBOUND-ONLY COLUMNS ─────────────────────────────────────────────────
    -- These are NULL for outbound messages.

    -- Query classification result from classifier.py.
    query_type              query_type,

    -- Composite confidence score from confidence.py (0.0–1.0).
    ai_confidence_score     DECIMAL(5, 4)
                                CHECK (ai_confidence_score IS NULL
                                    OR (ai_confidence_score >= 0 AND ai_confidence_score <= 1)),

    -- Full breakdown of all four confidence factors, stored for analysis.
    -- Allows us to query "which factor is driving low confidence?" in aggregate.
    confidence_factor_scores JSONB,

    -- The action the confidence engine recommended for this message.
    recommended_action      recommended_action,

    -- ── OUTBOUND-ONLY COLUMNS ────────────────────────────────────────────────
    -- These are NULL for inbound messages.

    -- Tracks how this outbound message was created and who sent it.
    draft_source            draft_source,

    -- The original AI-generated draft, preserved even if an agent edited it.
    -- Allows us to compare what the AI wrote vs. what was actually sent —
    -- this data trains future model improvements.
    original_ai_draft       TEXT,

    -- The agent who reviewed/edited/sent this message (NULL if auto-sent).
    sent_by_agent_id        VARCHAR(100),

    -- When the message was actually sent to the guest.
    -- NULL if still pending review or discarded.
    sent_at                 TIMESTAMPTZ,

    -- ── SHARED COLUMNS ───────────────────────────────────────────────────────

    -- When the message was created in our system (differs from sent_at for
    -- outbound messages that waited for agent review).
    created_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Core access patterns — indexed for the queries the agent dashboard will run:
CREATE INDEX idx_messages_conversation       ON messages (conversation_id, created_at DESC);
CREATE INDEX idx_messages_guest              ON messages (guest_id, created_at DESC);
CREATE INDEX idx_messages_direction          ON messages (direction);
CREATE INDEX idx_messages_query_type         ON messages (query_type) WHERE query_type IS NOT NULL;
CREATE INDEX idx_messages_confidence         ON messages (ai_confidence_score) WHERE ai_confidence_score IS NOT NULL;
CREATE INDEX idx_messages_recommended_action ON messages (recommended_action) WHERE recommended_action IS NOT NULL;
CREATE INDEX idx_messages_draft_source       ON messages (draft_source) WHERE draft_source IS NOT NULL;

COMMENT ON TABLE messages IS
    'Every inbound and outbound message across all channels in one table. '
    'Inbound messages have query_type, ai_confidence_score, and recommended_action populated. '
    'Outbound messages have draft_source, original_ai_draft, and sent_by_agent_id populated. '
    'original_ai_draft is preserved even after agent edits to enable model improvement analysis.';

COMMENT ON COLUMN messages.confidence_factor_scores IS
    'JSONB breakdown of the four confidence factors: '
    '{classification_confidence, sentiment_score, completeness_score, complexity_score}. '
    'Enables aggregate analysis of which factors most commonly drive escalation.';

COMMENT ON COLUMN messages.original_ai_draft IS
    'The Claude-generated draft before any agent editing. '
    'Never overwritten once set. Compared against final sent text to measure '
    'how much agents modify AI drafts — key signal for model quality improvement.';


-- ─────────────────────────────────────────────────────────────────────────────
-- UPDATED_AT TRIGGER FUNCTION
-- Automatically updates the updated_at timestamp on every row modification.
-- Without this, updated_at would have to be set by the application layer —
-- which is error-prone and easy to forget.
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
-- but has three different platform IDs. Storing all channel identities in a
-- separate table (rather than adding columns like airbnb_id, whatsapp_phone
-- to guests) means adding a new channel requires zero schema changes — just
-- a new enum value and new rows in channel_identities. The alternative (one
-- column per channel in guests) would require ALTER TABLE every time a new
-- channel is onboarded, and would result in a table with mostly NULL columns.
--
-- SECOND KEY DECISION: One messages table for all directions and channels.
-- The alternative was separate inbound_messages and outbound_messages tables.
-- One table is better because: conversation history queries join once instead
-- of twice, the chronological ordering of a thread is trivial (ORDER BY
-- created_at), and analytics across all messages (volume, response time,
-- confidence distributions) are single-table scans.
--
-- THIRD KEY DECISION: Preserving original_ai_draft even after agent edits.
-- This column is the foundation of a model improvement feedback loop. By
-- comparing original_ai_draft to message_text (the final sent version), we
-- can measure edit distance, identify which query types Claude handles poorly,
-- and build a fine-tuning dataset from high-quality agent corrections.
-- Without this column, that learning opportunity is permanently lost.
-- ─────────────────────────────────────────────────────────────────────────────