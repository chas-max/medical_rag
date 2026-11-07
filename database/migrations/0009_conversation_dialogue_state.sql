-- Owner-run only after 0001..0008. Adds the structured dialogue state to the
-- patient conversation row and the answer kind to its messages.
-- The existing conversation_scope policy (USING + WITH CHECK) already limits the
-- new UPDATE columns to the owning tenant/patient, so no wider policy is added.
-- Runtime roles receive column-level grants only; see patient_runtime_grants.sql.
BEGIN;

ALTER TABLE medical_rag.conversations
    ADD COLUMN dialogue_state jsonb,
    ADD COLUMN dialogue_revision bigint NOT NULL DEFAULT 0 CHECK (dialogue_revision >= 0),
    ADD COLUMN context_version bigint NOT NULL DEFAULT 0 CHECK (context_version >= 0),
    ADD CONSTRAINT conversation_dialogue_state_object CHECK (
        dialogue_state IS NULL OR (
            jsonb_typeof(dialogue_state) = 'object' AND octet_length(dialogue_state::text) <= 8192
        )
    );

ALTER TABLE medical_rag.conversation_turns
    ADD COLUMN answer_type text NOT NULL DEFAULT 'answer'
    CHECK (answer_type IN ('answer', 'clarification'));

-- Identity columns are immutable after insert: only the dialogue state and its
-- two version counters may change. SECURITY INVOKER keeps the caller's RLS and
-- privileges in force, so the trigger cannot become a privilege escalation path.
CREATE FUNCTION medical_rag.protect_conversation_identity() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog AS $$
BEGIN
    IF ROW(NEW.tenant_id,NEW.patient_id,NEW.conversation_id,NEW.title,NEW.created_at)
       IS DISTINCT FROM
       ROW(OLD.tenant_id,OLD.patient_id,OLD.conversation_id,OLD.title,OLD.created_at) THEN
        RAISE EXCEPTION 'Conversation identity is immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER protect_conversation_identity BEFORE UPDATE ON medical_rag.conversations
    FOR EACH ROW EXECUTE FUNCTION medical_rag.protect_conversation_identity();

COMMIT;
