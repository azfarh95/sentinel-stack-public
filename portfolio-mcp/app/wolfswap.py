"""WolfSwap PACK staking — on-chain reader.

Contract: 0xb54ca6af1fe0a65a7fed6eb2db45fb256acaab40 (Cronos)
  ERC-1967 proxy → implementation 0xe97fe4b5bac6e2e9d38a632240bc630cf6b34581
  - getStakingInfo(address user) selector 0xaa4704f3
    Returns 7+ uint256 words; word[0]=pending_rewards, word[3]=total_staked,
    word[5]=pool_TVL (in PACK, 18 decimals)
  - getUserStakesLength(address) -> uint256  selector 0x56f3d197
  - getUserStake(address, uint256 idx) -> (amount, ...)  selector 0xcec695fa

We only need word[3] (total staked) and word[0] (pending rewards).
"""
import logging
import httpx

logger = logging.getLogger(__name__)

CRONOS_RPC = "https://evm.cronos.org/"
STAKING_PROXY = "0xb54ca6af1fe0a65a7fed6eb2db45fb256acaab40"
GET_STAKING_INFO_SELECTOR = "0xaa4704f3"

# Word offsets in the getStakingInfo() return data
WORD_PENDING_REWARDS = 0
WORD_TOTAL_STAKED = 3
WORD_POOL_TVL = 5


class WolfSwapError(RuntimeError):
    pass


def _pad_addr(addr: str) -> str:
    return addr.lower().replace("0x", "").rjust(64, "0")


async def get_staking_info(user_address: str) -> dict:
    """Return {total_staked, pending_rewards, pool_tvl} all in PACK (float, 18 decimals).
    Raises WolfSwapError on RPC failure."""
    call_data = GET_STAKING_INFO_SELECTOR + _pad_addr(user_address)
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": STAKING_PROXY, "data": call_data}, "latest"],
    }
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(CRONOS_RPC, json=payload)
    j = r.json()
    if "error" in j:
        raise WolfSwapError(f"RPC error: {j['error']}")
    raw = j.get("result", "")
    if not raw or raw == "0x":
        raise WolfSwapError(f"empty result for {user_address}")

    # Strip 0x, split into 32-byte (64 hex char) words
    hex_data = raw[2:]
    n_words = len(hex_data) // 64
    if n_words <= WORD_POOL_TVL:
        raise WolfSwapError(f"unexpected return length: {n_words} words")
    words = [hex_data[i*64:(i+1)*64] for i in range(n_words)]

    def word_to_float(idx: int) -> float:
        return int(words[idx], 16) / 1e18

    return {
        "total_staked_pack": word_to_float(WORD_TOTAL_STAKED),
        "pending_rewards_pack": word_to_float(WORD_PENDING_REWARDS),
        "pool_tvl_pack": word_to_float(WORD_POOL_TVL),
    }
