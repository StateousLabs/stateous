"""Build time: `flow.py` -> the graph file -> a supervisor's `contract.py`.

It imports only `stateous.model`. The control plane and the supervisor never
import it; they read what it writes."""
