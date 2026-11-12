"""Execution: turns the brain's calls into orders.

Three books run side by side on every call:
  signal - the call itself, scored by the engine (always)
  paper  - simulated orders filled at live bid/ask (always; never affected by anything done in real)
  real   - orders on the owner's broker account (only when switched on, connected, funded and every check passes)
"""
