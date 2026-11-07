-- Owner-run only after 0001..0007. Adds the assistant side of a patient
-- conversation: every turn now records who produced it and, for model answers,
-- the bounded citation metadata returned by the retrieval workflow.
-- Trusted application code sets patient scope; this is not protection against
-- arbitrary runtime SQL or a DDL/schema owner.
BEGIN;
ALTER TABLE medical_rag.conversation_turns
    ADD COLUMN role text NOT NULL DEFAULT 'user',
    ADD COLUMN citations jsonb;
-- Turn order must not depend on wall-clock ties between a question and its
-- answer inserted in one transaction.
ALTER TABLE medical_rag.conversation_turns
    ADD COLUMN ordinal bigint GENERATED ALWAYS AS IDENTITY;
ALTER TABLE medical_rag.conversation_turns
    ADD CONSTRAINT conversation_turns_role_check CHECK (role IN ('user', 'assistant'));
ALTER TABLE medical_rag.conversation_turns
    ADD CONSTRAINT conversation_turns_citations_check CHECK (
        citations IS NULL OR jsonb_typeof(citations) = 'array');
-- One auditable action distinguishes "asked a question" from "stored a turn".
ALTER TABLE medical_rag.security_audit_events DROP CONSTRAINT security_audit_events_action_check;
ALTER TABLE medical_rag.security_audit_events ADD CONSTRAINT security_audit_events_action_check CHECK (action IN (
    'login.start','login.finish','session.read','logout','identity.read','consent.read','consent.grant',
    'consent.withdraw','conversation.create','conversation.list','conversation.read','turn.append','turn.list',
    'audit.read','request','provider.logout','support.create','support.list','support.read','support.cancel',
    'support.queue','support.take','support.resolve','chat.ask'));
COMMIT;
