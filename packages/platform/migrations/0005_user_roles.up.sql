-- 0005_user_roles -- PLAN-M0 task 7 (identity, roles, clearances, ACLs). 0002 created
-- `users` with department and clearance but no role, because 0002 was schema for task 5
-- ("users, tasks, journals") and role is task 7's own concern (0002's header comment says
-- exactly this: "this migration only creates the storage; task 7 does the identity logic").
--
-- `role` is a closed, small, operationally-meaningful vocabulary -- not a department, not
-- open text -- because registry/policy.yaml branches on its exact values today
-- (`actor.role: {in: [engineer, admin]}`), the same distinction
-- citadel_platform.registry.schema's module docstring draws between an open vocabulary
-- (never Literal[...]) and a closed one code is written against (CHECK-constrained here,
-- the same treatment already given to `clearance` and `tasks.classification` in 0002).
-- 'approver' is ADR-0001 §Q7's third demo identity; 'admin' is the value policy.yaml
-- already names alongside 'engineer' for execute-side-effect tools, seeded to nobody yet
-- (0006) but real vocabulary from day one, matching how tools.yaml declares tools this
-- repo does not implement yet.
--
-- No DEFAULT: every other column added this way in this repo (clearance, department,
-- external_identity, all in 0002) is NOT NULL with no default either, and `users` has zero
-- rows anywhere this migration has run (0006, which seeds the first three, is the next
-- migration in sequence) -- so there is nothing to backfill and nothing a silent default
-- could paper over.
--
-- `citadel_contracts.domain.User.roles` stays a tuple (plural) -- a user could in
-- principle hold more than one role, and the dataclass was ported already shaped that
-- way. This column is deliberately singular: the demo's three identities each need
-- exactly one, and a second role is not a case this repo has a design for yet. Whatever
-- code first reads a `users` row into a `User` (not written as of this migration) is
-- expected to wrap it as the one-element tuple `(row.role,)`, not to treat singular and
-- plural as a mismatch to paper over silently.

ALTER TABLE users
    ADD COLUMN role TEXT NOT NULL CHECK (role IN ('engineer', 'approver', 'admin'));

COMMENT ON COLUMN users.role IS
    'Closed vocabulary, checked here because registry/policy.yaml branches on it '
    '(actor.role). citadel_contracts.domain.User.roles is a tuple; this column is '
    'singular by design -- see this migration''s header comment.';
