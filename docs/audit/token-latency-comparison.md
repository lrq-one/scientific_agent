# Token and latency ledger

Historical traces contain call counts but not complete token or latency values.
The benchmark reports these as `not_measured`, never as zero. New isolated or
live runs should persist UsageCollector snapshots and projection ledgers, then
use the paired comparator. Real Qwen measurement remains blocked without
explicit authorization and budget.
