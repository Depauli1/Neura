# %% Hello
message = "Hello from Neura"
print(message)

# %% Persistent state
# Run this cell several times, then restart the runtime and run it again.
counter = globals().get("counter", 0) + 1
print(f"Run number: {counter}")
counter

# %% Streams
import sys
print("This goes to stdout")
print("This goes to stderr", file=sys.stderr)

# %% Error capture
# The error appears in Neura Outputs, without killing the session.
print("Output before the error is preserved")
raise ValueError("An intentional demo error")

# %% Interrupt
# Run this cell, then click Interrupt in Neura Outputs.
import time
print("Sleeping for 20 seconds — try Interrupt or Restart runtime")
time.sleep(20)
print("Finished sleeping")

# %% [markdown] Notes
# The commands execute code in the editor buffer, including unsaved changes.
# A non-empty selection executes exactly that text; an empty selection runs the current line.
# DataFrame previews, rich plots, notebooks, and AI chat are later milestones.
