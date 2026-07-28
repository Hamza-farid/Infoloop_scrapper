"""
CLI runner — test the scraper directly without Streamlit.
Usage:
    python run_cli.py 9107852362
    python run_cli.py 9107852362 9107852363 9107852364
"""

import asyncio
import sys
import os
import json

sys.path.insert(0, os.path.dirname(__file__))

from scraper.lookup import run_batch, lookup_phone


async def main():
    phones = sys.argv[1:] if len(sys.argv) > 1 else ["9107852362"]
    print(f"\n📞 CLI Lookup — {len(phones)} number(s): {phones}")

    if len(phones) == 1:
        from scraper.lookup import lookup_phone
        res = await lookup_phone(phones[0])
        print("\n" + "="*55)
        print(json.dumps(res.to_dict(), indent=2))
    else:
        results = await run_batch(phones)
        for r in results:
            print("\n" + "="*55)
            print(json.dumps(r.to_dict(), indent=2))

    print("\n✅ Done.")


if __name__ == "__main__":
    asyncio.run(main())
