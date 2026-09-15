# VERBATIM extract of `_recent_memory_preview`'s awk program from the memsearch plugin's
# hooks/session-start.sh (0.4.17-forge.2) -- the incumbent consumer of the daily journal.
#
# This file is a REFERENCE, not an implementation. `tests/test_journal.py` runs it against
# the same digests as `scribe.journal.preview` and asserts the two agree byte for byte. The
# point is not that scribe reuses awk; it is that the Python port cannot drift from the
# parser whose compatibility with scribe's digest format was measured rather than assumed.
#
# Do not "fix" anything here. A change that makes this file nicer makes it stop being
# evidence. It is pinned to the incumbent's behaviour, quirks included.
function flush_section() {
  if (section_len > 0 && has_body) {
    for (i = 1; i <= section_len; i++) {
      print section[i]
    }
  }
  delete section
  section_len = 0
  has_body = 0
}

/^##[[:space:]]/ {
  flush_section()
  section[++section_len] = $0
  next
}

/^#{3,4}[[:space:]]/ {
  section[++section_len] = $0
  next
}

/^-[[:space:]]/ {
  section[++section_len] = $0
  has_body = 1
  next
}

END {
  flush_section()
}
