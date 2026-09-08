#!/bin/bash
# Set the Railway variables that are not secret. Run after `railway login` and `railway link`.
#
# Deliberately does not touch the four that are: the RPC URL, the two keystores and their
# password. Those are pasted by hand into the Railway dashboard so they never pass through a
# shell history, a script, or this repository.
set -eu

railway variables \
  --set "WRASSE_ESCROW_ADDRESS=0x5525653f05990DA1479578893b5a624183AFa22E" \
  --set "WRASSE_BUYER_ADDRESS=0x30C95B7eb3E08F83992E803Be2A5AB0E0af93d22" \
  --set "WRASSE_PROVIDER_A_ADDRESS=0x0b920573ADf657f45Fecd9f7e48e66B5535A90C0" \
  --set "BASE_SEPOLIA_CHAIN_ID=84532" \
  --set "WRASSE_MEMORY_SOURCE_DIR=/data/source" \
  --set "WRASSE_BUYER_MEMORY_PATH=/data/work/buyer-memory.db" \
  --set "WRASSE_PROVIDER_MEMORY_PATH=/data/work/provider-memory.db" \
  --set "WRASSE_COLD_BUYER_MEMORY_PATH=/data/cold/buyer-memory.db" \
  --set "WRASSE_COLD_PROVIDER_MEMORY_PATH=/data/cold/provider-memory.db" \
  --set "WRASSE_TX_DB=/data/transactions.db" \
  --set "WRASSE_SESSION_ROOT=/data/sessions" \
  --set "WRASSE_ENABLE_EXECUTION=1"

echo
echo "Set. Four are still missing and are yours to paste in the dashboard:"
echo "  BASE_SEPOLIA_RPC_URL"
echo "  WRASSE_BUYER_KEYSTORE_JSON        the whole v3 keystore file, as one line"
echo "  WRASSE_PROVIDER_KEYSTORE_JSON     likewise"
echo "  WRASSE_KEYSTORE_PASSWORD          the password for both"
echo
echo "Then seed the volume, which must never come through the repository:"
echo "  tar -C .wrasse -czf - buyer-memory.db provider-memory.db \\"
echo "    | railway run -- sh -c 'mkdir -p /data/source && tar -C /data/source -xzf -'"
