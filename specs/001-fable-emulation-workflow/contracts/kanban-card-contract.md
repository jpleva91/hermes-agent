# Contract: Kanban Card for Fable-Emulation Workflow

Every implementation, research, synthesis, or review card groomed from this spec must include the following fields in the task body or structured metadata.

## Required fields

- **Spec reference**: `specs/001-fable-emulation-workflow/{spec,plan,tasks}.md`
- **User story / task IDs**: e.g. `US1`, `T001`
- **Assignee**: verified profile or explicit external poller/human
- **Workspace**: `dir:/absolute/path`, `worktree`, or explicit no-files-needed handoff
- **Parent dependencies**: actual Kanban parent IDs, not prose-only dependency notes
- **Acceptance criteria**: concrete pass/fail bullets
- **Evidence required**: commands, logs, screenshots, citations, paths, or review verdict
- **Side-effect authority**: `none`, `verify-only`, `may-write-local`, `may-open-pr`, `requires-Jared-approval`
- **Completion handoff**: expected summary/comment shape for downstream cards

## Invalid card states

- Assignee does not exist and no human/external poller is documented.
- Downstream artifacts are expected from scratch workspace without copy-out or embedded result.
- Review/merge/deploy is implied but not explicitly authorized.
- Parent dependencies are described in prose but not linked in Kanban.
- Acceptance criteria say only "works" or "done" without evidence.
