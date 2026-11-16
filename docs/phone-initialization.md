# Initial phone inventory

`init-phone PROFILE` records the first complete SHA-256 inventory of the registered phone's configured camera directory through ADB. The first successful pass fixes the list of regular files and their content for this profile. Files added later are outside this baseline. An interrupted or failed phone read does not save a partial baseline: retry the initial phone scan. After the complete baseline is saved, comparison and reporting no longer need access to the phone.

For example, with profile `phone-a` assigned to the `data` archive group:

```bash
media-archive init-phone phone-a
media-archive inventory --hash-local
media-archive audit --mode full --group data --targets a b
media-archive init-phone phone-a --audit AUDIT_ID
```

The source profile determines the collection and the policy. Comparison requires a completed local inventory with SHA-256 for its collection and a completed full audit of two physically distinct, currently registered volume episodes from the collection's archive group. The audit ID selects the pair explicitly. Archive disks can have been read sequentially and need not be connected during comparison. An audit that found differences can supply evidence if both inventories are complete. Quick scans and incomplete full reads cannot support decisions. Results refer to the times of the saved reads and are not a fresh guarantee of presence.

The comparison matches content by SHA-256 and size, including copies with different filenames. It uses local occurrences owned by the configured collection and archive occurrences within that collection's registered destination. An occurrence belonging to another collection does not satisfy the profile's requirement. It records:

- **`baseline_skip_missing`:** keep matches observed locally or on the selected archives; record a baseline skip for files present only on the phone within the compared scope. The profile can now finish initialization.
- **`import_missing`:** recognize matches in the local collection. Files without a local match stay pending for the later import workflow, even if an archive copy was observed. If there is nothing to fill, initialization can finish.

The command only reads media. It does not create import, placement, or backup success entries. Policy and the source device at the time of capture remain in the history. The command does not implement incremental phone import, camera initialization or a baseline reset.

## Progress, records and reports

Exit code `3` means some work remains: a prerequisite could not be read, the audit ID is still needed, or later import must fill local gaps. Code `0` means the profile's initialization is complete. Ctrl+C returns `130`. Report pairs contain the phone baseline ID, status, and per-file decisions with evidence references after comparison.

The workflow uses the existing [initialization state](history.md#initialization). The boundary stores the phone root and device ID. The initial run's source observations form the manifest. The completed `copies` comparison scope stores the local run and audit IDs plus a decision per source occurrence. It retains local occurrence IDs and archive paths, volume IDs and read times. Decisions and any baseline skips commit together. The `baseline` processing scope remains open when local copies are still missing. No new SQLite revision is needed.

[Synthetic integration tests](../tests/test_inventory.py) cover both policies, archives read one at a time, incomplete evidence, interruption, and files added after the baseline was captured.
