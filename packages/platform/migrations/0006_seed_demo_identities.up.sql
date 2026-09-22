-- 0006_seed_demo_identities -- PLAN-M0 task 7's "Done" bar, literally: "Seed three
-- identities for the demonstration: two engineers at different classification levels in
-- different departments, and one approver." ADR-0001 §Q7: "Minimum cast for the demo: two
-- engineers at different classification levels in different departments, and one
-- approver. Three identities." This migration is that cast, and only that cast -- no other
-- rows, no other tables.
--
-- Seed DATA in a migration, not schema, is a deliberate departure from 0001-0005's pattern
-- (every earlier migration is pure DDL). Justified here, specifically: these three rows
-- are not sample data to delete before a real deployment, they ARE the fixed demonstration
-- cast ADR-0001 names by name-shape ("two engineers... one approver"), so seeding them
-- through the same forward/back mechanism as everything else means one command
-- (`python -m citadel_platform.migrations up`) leaves a freshly-initialised database ready
-- for the demonstration, and `down` removes exactly these three rows and nothing else --
-- both proven the same way 0001-0004 already are, by citadel_platform.migrations actually
-- running this forward and back against a real database. This is not a precedent for
-- seeding arbitrary data as migrations; a future real user roster belongs in a signup flow
-- or an admin tool, not another numbered migration.
--
-- Fictional names, deliberately -- these are demo personas, not any real person, so an
-- audit-chain entry attributed to "R. Kulkarni" during the demonstration is never
-- mistakable for an entry attributed to whoever is running or watching it.
--
-- external_identity is what a real session token's `sub` claim would carry (0002's own
-- comment on that column) -- 'demo-engineer-1' etc. stand in for that until a real
-- identity provider is wired up (out of scope for M0).

INSERT INTO users (display_name, department, clearance, role, external_identity) VALUES
    ('R. Kulkarni', 'process-engineering', 'internal',     'engineer', 'demo-engineer-1'),
    ('S. Nair',     'instrumentation',     'confidential', 'engineer', 'demo-engineer-2'),
    ('V. Rangan',   'quality-assurance',   'confidential', 'approver', 'demo-approver');
