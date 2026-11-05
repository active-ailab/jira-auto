# Jira Auto service

- This is an independent service repository inside the dedicated firmware repo workspace.
- Keep issue reads and comments on the installed Jira CLI; never implement a second credential store. The user explicitly requires quality archives attached to Jira. Since this CLI has no attachment upload command, only that operation may use fixed-origin curl multipart with existing netrc, durable receipts and no redirects or automatic retry after unknown outcomes.
- Never commit credentials, `.env`, `data/`, real logs, downloaded binaries, or database files.
- User authorized LAN access without username/password login for now. Keep JA_LAN_AUTH=0; retain explicit Host/Origin allowlists, CSRF protection and human approval of Jira comments. Do not expose publicly.
- A rules-only material inventory is not a root-cause conclusion. Mark unimplemented integrations honestly.
- MCP tools must not expose approvals, publication, transitions, arbitrary shell commands, or arbitrary file access.
- Reports require human approval of a concrete revision; issue and evidence changes invalidate it.
- Never test publication on real Jira. Use fake clients in tests; leave writes disabled by default.
- Do not run destructive firmware cleanup commands from this service. Source comparison is read-only in v0.1.
- Canonical MCP source is `plugin/bridge.py`; sync its packaged copy `jira_service/mcp_bridge.py` after changes.
- Run `.venv/bin/pytest -q` and `node --check jira_service/static/app.js` after relevant changes.

- AI runs use the user's logged-in Codex CLI with execution/MCP/browser/plugin tools disabled. Only curated evidence is passed; do not persist internal reasoning items.
- Insufficient-evidence and undetermined results must produce reviewable drafts. Model execution failure is distinct and must not be presented as a successful root-cause analysis.
- When Jira writes are intentionally enabled, the user's approval may publish that exact draft in one action. Never approve a real draft during verification.
- New AI comments briefly describe the problem sequence, then evidence-based cause assessment, then a short conclusion. Cite the process and cause claims; reboot counts must state their input scope and separate reset type from trigger. Avoid repeating the same facts in every paragraph. Keep necessary caveats and bounded similar-issue suggestions; full analysis stays on the dashboard. Legacy saved drafts retain their original layout until explicitly revised.
- Model execution failure is kind analysis_failed: retain a visible reviewable record, but disable approvals and do not call it evidence insufficiency. Feishu failure cards have no approval controls.
- Codex structured output schemas must recursively require every object property, including defaulted Pydantic fields; test the schema before changing result fields.
- Similar-issue lookup uses Jira CLI and includes recently updated resolved bugs. Existing comments are attributed leads, not fresh dump verification. Never auto-link duplicates or transition issues.

- Preflight and AI runs must download and validate supported Jira attachments before analyzing. Download failures are processing failures, not missing user evidence. Jira CLI lacks file downloads in this environment; attachment-only curl uses existing netrc against fixed Jira origin, without redirects or a second credential store.
- Dump preparation must label actual parser coverage. Geneva SRAM descriptor/code comparison does not imply all threads, exception registers or a complete call stack were decoded. Never reuse another issue's hardcoded crash addresses or conclusions.

- For crashes, review and comment must begin with core raw fault/abort logs and symbolized call path, followed by code-based mechanism and limited unresolved cause. Keep full frame addresses and manifest source provenance in the dashboard. Device addr2line lists can contain bare hex and stack-scan candidates; exclude normal power-management backtraces and don't merge later boot warnings. Use LF line numbering consistently.
- P0 checkpoints are keyed by inputs plus parser version and validated output hashes. Retry only read/preparation/analysis stages; never enqueue approval or publication from a retry.
- Analyze only a selected scene's LF range and associated dump. Time-window pairing is evidence, not full build proof; ambiguous pairing pauses automatic selection. Never mix unknown dump groups.
- Zepp #CD v1 decoding is bounded and layout/version checked. Unsupported/truncated coredumps remain explicitly unparsed. Single-thread registers do not imply all-thread/heap recovery.
- Keep generated forensic artifacts immutable and tied to scene ID; older generated scene reports must not contaminate current evidence. Preserve successful model calls only with exact prompt/evidence/schema/model fingerprints and validated references.
- Review exposes direct mechanism and underlying cause independently, with evidence references. Historical prose must not gain an inferred confirmation grade. Historical revisions are read-only; editing must atomically verify the latest source report and issue fingerprint.
- A retry/analysis must use the exact forensic artifact ID returned by its preparation stage and validate its SHA256. Same scene ID alone does not identify the parser output. Evidence budgets must retain build comparison and parser limitations before optional frames.
- Queue deduplication must reject differing scene/version/review options. Cancellation is restricted to unclaimed queued tasks; it never cancels publication or silently interrupts active tools.
- Run `node --check jira_service/static/review.js` in addition to the app.js syntax check after frontend changes.
- Screenshot inputs are bounded immutable raster files (PNG/JPEG/WebP), supplied natively to the model in explicit evidence order; cache keys include their hashes. Record omitted/invalid screenshots and never claim all images were viewed. Static images do not prove crash duration, heap state, or association to the selected scene.
- Fault-access arithmetic supports only declared RV32 load/store forms with matching saved/log PC. It must preserve separate code-byte matching and lifecycle limitations; abort PCs and adjusted SP must not become invented illegal accesses.
- Publication reconciliation is read-only on Jira and never retries comments. Restore success only on one standalone report marker plus the exact original approved characters, allowing only CLI-added Wiki escaping and CRLF normalization. Missing markers, changed bodies, duplicate markers or incomplete pages do not prove failure to publish.
- Existing comments are bounded, attributed historical leads. Preserve distinct comment IDs and source path/revision when deduplicating evidence; identical text is not necessarily the same evidence source.
- Reset classification and physical trigger are separate. Parse the actual trailing reset value using the log's own legend; enum words are not crash events. POR plus a nearby reboot menu is an association, not proof of a held key, water ingress, user error or a particular elapsed duration. Preserve partial-excerpt provenance and supplied original line numbers.

- Inspect every attachment member by content and record its role/value/parse status. Rare invalid UTF-8 bytes must not hide an otherwise readable firmware log. Preserve original bytes and LF line numbering; replacement characters are not exact evidence. Distinguish mobile logs, Bluetooth traces and damaged archives from firmware logs or ramdump. Check existing hold events and software reboot requests before asking for more logs. Incomplete ZIP recovery is not CRC-verified evidence.

- Quality feedback with no collected material must stop at material collection, not produce an approvable no-log analysis. Quality ZIP import must archive the original package in Jira, refresh attachment metadata through CLI, then enqueue analysis. iOS device packages may have four nested ZIP layers; retain total byte/file limits, CRC and path validation.

- Verified GenevaW uses CI Geneva artifact naming; retain the reported device and independently check code bytes. Support the known Jenkins HuamiOS_HS3 workspace root for manifest-scoped source reads. If later coredump memory hex is corrupt, only a preceding complete validated RV32 register block may be recovered as registers_only; never claim memory decode or accept known multiple register blocks. Fault arithmetic still requires logged/saved PC agreement.

- For Zepp/Huami Bug triage, quality feedback collection or workflow troubleshooting, proactively use the reusable skill at `plugin/skills/zepp-jira-triage/SKILL.md`. Follow its collection → Jira archive → per-file inspection → version/scene/code analysis → human review flow without waiting for the user to restate the steps. It does not apply to ordinary task scheduling or account matching, and does not authorize approval/publication.

- Daily focus reminders use Asia/Shanghai 09:00 and at most three assigned unresolved issues per day, ordered by due date, priority, start date. Freeze the day's slots; completed/reassigned/future-rescheduled items hide without refill. Only fresh complete assigned snapshots may trigger Feishu delivery; stale/partial sync cannot. Reminder receipts are unique by local date, sends claimed atomically, unknown outcomes never auto-replayed. Reminder cards cannot approve, comment or transition Jira.

- Exclude all issue types whose deadline is strictly earlier than six calendar months before the Shanghai local date from daily focus. Keep the exact six-month boundary, undated issues. Policy upgrades may rebuild today's slots while preserving sent/unknown delivery receipts; never resend to apply a new recommendation rule.
