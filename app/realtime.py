import asyncio
import json
from typing import Any, Dict, List, Optional, Set, Tuple


class RealtimeBroker:
    """In-memory async SSE broadcaster for live Cashier, Kitchen (KDS), Admin & Customer tracking."""

    def __init__(self) -> None:
        self._subscribers: Set[Tuple[asyncio.Queue, Optional[int], Optional[int], Optional[str]]] = set()

    def subscribe(
        self,
        brand_id: Optional[int] = None,
        branch_id: Optional[int] = None,
        order_number: Optional[str] = None,
    ) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.add((q, brand_id, branch_id, order_number))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        to_remove = [sub for sub in self._subscribers if sub[0] is q]
        for sub in to_remove:
            self._subscribers.discard(sub)

    async def publish(
        self,
        event: str,
        data: Dict[str, Any],
        brand_id: Optional[int] = None,
        branch_id: Optional[int] = None,
        order_number: Optional[str] = None,
    ) -> None:
        payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        dead: List[Tuple[asyncio.Queue, Optional[int], Optional[int], Optional[str]]] = []
        for sub in list(self._subscribers):
            q, sub_brand, sub_branch, sub_order = sub
            if sub_brand is not None and brand_id is not None and sub_brand != brand_id:
                continue
            if sub_branch is not None and branch_id is not None and sub_branch != branch_id:
                continue
            if sub_order is not None and order_number is not None and sub_order != order_number:
                continue
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                dead.append(sub)
        for sub in dead:
            self._subscribers.discard(sub)


broker = RealtimeBroker()
