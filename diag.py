import asyncio
import logging
from collections import Counter

from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.WARNING)

from app.backend.services.discovery_service._engine import aggregate_ideas
from app.backend.services.discovery_service._sources import SOURCES

r = asyncio.run(aggregate_ideas())
print(f'Total: {len(r.ideas)}  Regime: {r.regime.mode}')
sc: Counter[str] = Counter()
for i in r.ideas:
    for s in i.signals:
        sc[s.source] += 1
print()
print('Per-source contribution:')
for src, _ in SOURCES:
    n = sc.get(src, 0)
    marker = '⚠ ZERO' if n == 0 else ''
    print(f'  {src:.<28} {n:>4d}  {marker}')