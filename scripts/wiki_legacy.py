#!/usr/bin/env python3
"""Constants of the legacy resolution channel (docking spec, step 5).

The pre-kit deployment publishes its wiki root through an environment
variable whose name carries the source family string. The resolver
honors that channel read-only so the pre-kit install keeps working
until an adoption ruling retires it. Like the event store's envelope
key, the name travels as a code constant in this sweep-enclave module:
never config, never derived, never widened.
"""

# The v1 deployment's env channel name (read-only; retired at adoption).
LEGACY_WIKI_ENV = "AURA_WIKI"  # v1 legacy shim constant, see module docstring
