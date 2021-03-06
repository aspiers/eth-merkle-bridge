------------------------------------------------------------------------------
-- Oracle contract
------------------------------------------------------------------------------

-- Internal type check function
-- @type internal
-- @param x variable to check
-- @param t (string) expected type
local function _typecheck(x, t)
  if (x and t == 'address') then
    assert(type(x) == 'string', "address must be string type")
    -- check address length
    assert(52 == #x, string.format("invalid address length: %s (%s)", x, #x))
    -- check character
    local invalidChar = string.match(x, '[^123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz]')
    assert(nil == invalidChar, string.format("invalid address format: %s contains invalid char %s", x, invalidChar or 'nil'))
  else
    -- check default lua types
    assert(type(x) == t, string.format("invalid type: %s != %s", type(x), t or 'nil'))
  end
end

state.var {
    -- Global State root included in block headers
    -- (0x hex string)
    _anchorRoot = state.value(),
    -- Height of the last block anchored
    -- (uint)
    _anchorHeight = state.value(),

    -- _tAnchor is the anchoring periode: sets a minimal delay between anchors to prevent spamming
    -- and give time to applications to build merkle proof for their data.
    -- (uint)
    _tAnchor = state.value(),
    -- _tFinal is the time after which the validators considere a block finalised
    -- this value is only useful if the anchored chain doesn't have LIB
    -- Since Aergo has LIB it is a simple indicator for wallets.
    -- (uint)
    _tFinal = state.value(),
    -- _validators contains the addresses and 2/3 of them must sign a root update
    -- The index of validators starts at 1.
    -- (uint) -> (address) 
    _validators = state.map(),
    -- Number of validators registered in the Validators map
    -- (uint)
    _validatorsCount = state.value(),
    -- _nonce is a replay protection for validator and root updates.
    -- (uint)
    _nonce = state.value(),
    -- _contractId is a replay protection between sidechains as the same addresses can be validators
    -- on multiple chains.
    -- (string)
    _contractId = state.value(),
    -- address of the bridge contract being controlled by oracle
    _bridge = state.value(),
    -- Address of bridge contract on Ethereum: used to relay the Eth bridge contract storage root on Aergo.
    -- (0x hex string)
    _destinationBridgeAddr = state.value(),
}

--------------------- Utility Functions -------------------------
-- Check 2/3 validators signed message hash
-- @type    query
-- @param   hash (0x hex string)
-- @param   signers ([]uint) array of signer indexes
-- @param   signatures ([]0x hex string) array of signatures matching signers indexes
-- @return  (bool) 2/3 signarures are valid
function validateSignatures(hash, signers, signatures)
    -- 2/3 of Validators must sign for the hash to be valid
    nb = _validatorsCount:get()
    assert(nb*2 <= #signers*3, "2/3 validators must sign")
    for i,signer in ipairs(signers) do
        if i > 1 then
            assert(signer > signers[i-1], "All signers must be different")
        end
        assert(_validators[signer], "Signer index not registered")
        assert(crypto.ecverify(hash, signatures[i], _validators[signer]), "Invalid signature")
    end
    return true
end

-- Concatenate strings in array
-- @type    query
-- @param   array ([]string)
-- @return  (string)
function join(array)
    -- not using a separator is safe for signing if the length of items is checked with isValidAddress for example
    str = ""
    for i, data in ipairs(array) do
        str = str..data
    end
    return str
end

-- Verify a Merkle proof of Eth state object inside anchored root
-- @type    query
-- @param   nonce (0x hex string) Bridge contract nonce
-- @param   balance (0x hex string) Bridge contract balance
-- @param   root (0x hex string) Bridge contract storage root
-- @param   codeHash (0x hex string) Bridge contract code hash
-- @param   mp - merkle proof of inclusion of proto serialized account in general trie
-- @param   merkleProof ([]0x hex string) merkle proof of inclusion of RLP serialized account in general trie
-- @returns (bool) True if the proof is valid
function verifyEthStateProof(nonce, balance, root, codeHash, merkleProof)
    local accountState = {nonce, balance, root, codeHash}
    return crypto.verifyProof(_destinationBridgeAddr:get(), accountState, _anchorRoot:get(), unpack(merkleProof))
end

-- Create a new oracle contract
-- @type    __init__
-- @param   validators ([]address) array of Aergo addresses
-- @param   bridge (address) address of already deployed bridge contract
-- @param   destinationBridgeAddr (0x hex string) trie key of destination bridge contract in Ethereum state trie
-- @param   tAnchor (uint) anchoring periode on this contract
-- @param   tFinal (uint) finality of anchored chain
-- @return  (string) id of contract
function constructor(validators, bridge, destinationBridgeAddr, tAnchor, tFinal)
    _nonce:set(0)
    _validatorsCount:set(#validators)
    for i, addr in ipairs(validators) do
        _typecheck(addr, 'address')
        _validators[i] = addr
    end
    _bridge:set(bridge)
    _destinationBridgeAddr:set(destinationBridgeAddr)
    _tAnchor:set(tAnchor)
    _tFinal:set(tFinal)
    _anchorRoot:set("constructor")
    _anchorHeight:set(0)
    -- contractID is the hash of system.getContractID (prevent replay between contracts on the same chain) and system.getPrevBlockHash (prevent replay between sidechains).
    -- take the first 16 bytes
    local id = crypto.sha256(system.getContractID()..system.getPrevBlockHash())
    id = string.sub(id, 3, 34)
    _contractId:set(id)
    return id
end

-- Getter for validators
-- @type    query
-- @return  ([]string) array or validator addresses
function getValidators()
    local validators = {}
    for i=1, _validatorsCount:get() do
        validators[i] = _validators[i]
    end
    return validators
end

-- Getter for anchored state root and height
-- @type    query
-- @return  (0x hex string, int) state root, height of the anchored blockchain
function getForeignBlockchainState()
    return _anchorRoot:get(), _anchorHeight:get()
end

-- Register a new set of validators
-- @type    call
-- @param   validators ([]address) array of Aergo addresses
-- @param   signers ([]uint) array of signer indexes
-- @param   signatures ([]0x hex string) array of signatures matching signers indexes
-- @event   validatorsUpdate(proposer, new validators)
function validatorsUpdate(validators, signers, signatures)
    oldNonce = _nonce:get()
    -- it is safe to join validators without a ',' because the validators length is checked in _typecheck
    message = crypto.sha256(join(validators)..tostring(oldNonce).._contractId:get().."V")
    assert(validateSignatures(message, signers, signatures), "Failed new validators signature validation")
    oldCount = _validatorsCount:get()
    if #validators < oldCount then
        diff = oldCount - #validators
        for i = 1, diff+1, 1 do
            -- delete validator slot
            _validators:delete(oldCount + i)
        end
    end
    _validatorsCount:set(#validators)
    for i, addr in ipairs(validators) do
        -- NOTE if length of addresses is not checked with _typecheck, then array items must be separated by a separator
        _typecheck(addr, 'address')
        _validators[i] = addr
    end
    _nonce:set(oldNonce + 1)
    contract.event("validatorsUpdate", system.getSender(), unpack(validators))
end

-- Replace the oracle with another one
-- @type    call
-- @param   newOracle (address) Aergo address of the new oracle
function oracleUpdate(newOracle, signers, signatures)
    oldNonce = _nonce:get()
    message = crypto.sha256(newOracle..tostring(oldNonce).._contractId:get().."O")
    assert(validateSignatures(message, signers, signatures), "Failed new oracle signature validation")
    _nonce:set(oldNonce + 1)
    contract.call(_bridge:get(), "oracleUpdate", newOracle)
end

-- Register new anchoring periode
-- @type    call
-- @param   tAnchor (uint) new anchoring periode
-- @param   signers ([]uint) array of signer indexes
-- @param   signatures ([]0x hex string) array of signatures matching signers indexes
function tAnchorUpdate(tAnchor, signers, signatures)
    oldNonce = _nonce:get()
    message = crypto.sha256(tostring(tAnchor)..tostring(oldNonce).._contractId:get().."A")
    assert(validateSignatures(message, signers, signatures), "Failed tAnchor signature validation")
    _nonce:set(oldNonce + 1)
    _tAnchor:set(tAnchor)
    contract.call(_bridge:get(), "tAnchorUpdate", tAnchor)
end

-- Register new finality of anchored chain
-- @type    call
-- @param   tFinal (uint) new finality of anchored chain
-- @param   signers ([]uint) array of signer indexes
-- @param   signatures ([]0x hex string) array of signatures matching signers indexes
function tFinalUpdate(tFinal, signers, signatures)
    oldNonce = _nonce:get()
    message = crypto.sha256(tostring(tFinal)..tostring(oldNonce).._contractId:get().."F")
    assert(validateSignatures(message, signers, signatures), "Failed tFinal signature validation")
    _nonce:set(oldNonce + 1)
    _tFinal:set(tFinal)
    contract.call(_bridge:get(), "tFinalUpdate", tFinal)
end

-- Register new unfreezing fee for delegated unfreeze service
-- @type    call
-- @param   fee (ubig) new unfreeze fee
-- @param   signers ([]uint) array of signer indexes
-- @param   signatures ([]0x hex string) array of signatures matching signers indexes
function unfreezeFeeUpdate(fee, signers, signatures)
    oldNonce = _nonce:get()
    message = crypto.sha256(bignum.tostring(fee)..tostring(oldNonce).._contractId:get().."UF")
    assert(validateSignatures(message, signers, signatures), "Failed unfreeze fee signature validation")
    _nonce:set(oldNonce + 1)
    contract.call(_bridge:get(), "unfreezeFeeUpdate", fee)
end

-- Register a new state anchor
-- @type    call
-- @param   root (0x hex string) Ethereum state root
-- @param   height (uint) block height of root
-- @param   signers ([]uint) array of signer indexes
-- @param   signatures ([]0x hex string) array of signatures matching signers indexes
function newStateAnchor(root, height, signers, signatures)
    assert(height > _anchorHeight:get() + _tAnchor:get(), "Next anchor height not reached")
    oldNonce = _nonce:get()
    message = crypto.sha256(string.sub(root, 3)..','..tostring(height)..tostring(oldNonce).._contractId:get().."R")
    assert(validateSignatures(message, signers, signatures), "Failed signature validation")
    _nonce:set(oldNonce + 1)
    _anchorRoot:set(root)
    _anchorHeight:set(height)
    contract.event("newAnchor", system.getSender(), height, root)
end

-- Register a new bridge anchor
-- @type    call
-- @param   nonce (0x hex string) Bridge contract nonce
-- @param   balance (0x hex string) Bridge contract balance
-- @param   root (0x hex string) Bridge contract storage root
-- @param   codeHash (0x hex string) Bridge contract code hash
-- @param   mp - merkle proof of inclusion of proto serialized account in general trie
-- @param   merkleProof ([]0x hex string) merkle proof of inclusion of RLP serialized account in general trie
function newBridgeAnchor(nonce, balance, root, codeHash, merkleProof)
    if not verifyEthStateProof(nonce, balance, root, codeHash, merkleProof) then
        error("Failed to verify bridge state inside general state")
    end
    contract.call(_bridge:get(), "newAnchor", root, _anchorHeight:get())
end

-- Register a new state anchor and update the bridge anchor
-- @type    call
-- @param   stateRoot (0x hex string) Ethereum state root
-- @param   height (uint) block height of root
-- @param   signers ([]uint) array of signer indexes
-- @param   signatures ([]0x hex string) array of signatures matching signers indexes
-- @param   bridgeNonce (0x hex string) Bridge contract nonce
-- @param   bridgeBalance (0x hex string) Bridge contract balance
-- @param   bridgeRoot (0x hex string) Bridge contract storage root
-- @param   bridgeCodeHash (0x hex string) Bridge contract code hash
-- @param   merkleProof ([]0x hex string) merkle proof of inclusion of RLP serialized account in general trie
function newStateAndBridgeAnchor(stateRoot, height, signers, signatures, bridgeNonce, bridgeBalance, bridgeRoot, bridgeCodeHash, merkleProof)
    newStateAnchor(stateRoot, height, signers, signatures)
    newBridgeAnchor(bridgeNonce, bridgeBalance, bridgeRoot, bridgeCodeHash, merkleProof)
end

abi.register(validateSignatures, join, verifyEthStateProof, getValidators, getForeignBlockchainState, validatorsUpdate, oracleUpdate, tAnchorUpdate, tFinalUpdate, unfreezeFeeUpdate, newStateAnchor, newBridgeAnchor, newStateAndBridgeAnchor)