import argparse
from functools import (
    partial,
)
from getpass import getpass
import grpc
import hashlib
import json
from multiprocessing.dummy import (
    Pool,
)
import time
import threading

from typing import (
    Tuple,
    Optional,
    List,
    Any,
    Dict
)

import aergo.herapy as herapy
from aergo.herapy.utils.signature import (
    verify_sig,
)

from ethaergo_bridge_operator.bridge_operator_pb2_grpc import (
    BridgeOperatorStub,
)
from ethaergo_bridge_operator.bridge_operator_pb2 import (
    Anchor,
    NewValidators,
    NewTempo,
    NewUnfreezeFee,

)
from ethaergo_bridge_operator.op_utils import (
    query_aergo_tempo,
    query_aergo_validators,
    query_aergo_id,
    query_unfreeze_fee,
)
from ethaergo_bridge_operator.exceptions import (
    ValidatorMajorityError,
)
from web3 import (
    Web3,
)
from web3.middleware import (
    geth_poa_middleware,
)


_ONE_DAY_IN_SECONDS = 60 * 60 * 24


class AergoProposerClient(threading.Thread):
    """The bridge proposer periodically (every t_anchor) broadcasts
    the finalized trie state root (after lib) of the bridge contract
    on both sides of the bridge after validation by the Validator servers.
    It first checks the last merged height and waits until
    now > lib + t_anchor is reached, then merges the current finalised
    block (lib). Start again after waiting t_anchor.

    Note on config_data:
        - config_data is used to store current validators and their ip when the
          proposer starts. (change validators after the proposer has started)
        - After starting, when users change the config.json, the proposer will
          attempt to gather signatures to reflect the changes.
        - t_anchor value is always taken from the bridge contract
        - validators are taken from the config_data because ip information is
          not stored on chain
        - when a validator set update succeeds, self.config_data is updated
        - if another proposer updates to a new set of validators and the
          proposer doesnt know about it, proposer must be restarted with the
          new current validator set to create new connections to them.

    """

    def __init__(
        self,
        config_file_path: str,
        aergo_net: str,
        eth_net: str,
        eth_block_time: int,
        privkey_name: str = None,
        privkey_pwd: str = None,
        tab: str = "",
        auto_update: bool = False,
        aergo_gas_price: int = None
    ) -> None:
        threading.Thread.__init__(self)
        if aergo_gas_price is None:
            aergo_gas_price = 0
        self.aergo_gas_price = aergo_gas_price
        self.config_file_path = config_file_path
        self.config_data = self.load_config_data()
        self.eth_block_time = eth_block_time
        self.tab = tab
        self.eth_net = eth_net
        self.aergo_net = aergo_net
        self.auto_update = auto_update
        print("------ Connect Aergo and Ethereum -----------")
        self.hera = herapy.Aergo()
        self.hera.connect(self.config_data['networks'][aergo_net]['ip'])

        ip = self.config_data['networks'][eth_net]['ip']
        self.web3 = Web3(Web3.HTTPProvider(ip))
        eth_poa = self.config_data['networks'][eth_net]['isPOA']
        if eth_poa:
            self.web3.middleware_onion.inject(geth_poa_middleware, layer=0)
        assert self.web3.isConnected()

        self.eth_bridge = (self.config_data['networks'][eth_net]['bridges']
                           [aergo_net]['addr'])
        self.aergo_bridge = (self.config_data['networks'][aergo_net]['bridges']
                             [eth_net]['addr'])
        self.aergo_id = query_aergo_id(self.hera, self.aergo_bridge)

        print("------ Connect to Validators -----------")
        validators = query_aergo_validators(self.hera, self.aergo_bridge)
        print("Validators: ", validators)
        # create all channels with validators
        self.channels: List[grpc._channel.Channel] = []
        self.stubs: List[BridgeOperatorStub] = []
        assert len(validators) == len(self.config_data['validators']), \
            "Validators in config file must match bridge validators " \
            "when starting (current validators connection needed to make "\
            "updates).\nExpected validators: {}".format(validators)
        for i, validator in enumerate(self.config_data['validators']):
            assert validators[i] == validator['addr'], \
                "Validators in config file must match bridge validators " \
                "when starting (current validators connection needed to make "\
                "updates).\nExpected validators: {}".format(validators)
            ip = validator['ip']
            channel = grpc.insecure_channel(ip)
            stub = BridgeOperatorStub(channel)
            self.channels.append(channel)
            self.stubs.append(stub)

        self.pool = Pool(len(self.stubs))

        # get the current t_anchor and t_final for both sides of bridge
        self.t_anchor, self.t_final = query_aergo_tempo(
            self.hera, self.aergo_bridge
        )
        print("{}              <- {} (t_final={}) : t_anchor={}"
              .format(aergo_net, eth_net, self.t_final, self.t_anchor))

        print("------ Set Sender Account -----------")
        if privkey_name is None:
            privkey_name = 'proposer'
        if privkey_pwd is None:
            privkey_pwd = getpass("Decrypt exported private key '{}'\n"
                                  "Password: ".format(privkey_name))
        sender_priv_key = self.config_data['wallet'][privkey_name]['priv_key']
        self.hera.import_account(sender_priv_key, privkey_pwd)
        print("  > Proposer Address: {}".format(self.hera.account.address))

    def get_anchor_signatures(
        self,
        root: str,
        merge_height: int,
        nonce: int,
    ) -> Tuple[List[str], List[int]]:
        """ Query all validators and gather 2/3 of their signatures. """

        # messages to get signed
        msg_str = root + ',' + str(merge_height) + str(nonce) \
            + self.aergo_id + "R"
        msg = bytes(msg_str, 'utf-8')
        h = hashlib.sha256(msg).digest()

        anchor = Anchor(
            root=bytes.fromhex(root), height=merge_height,
            destination_nonce=nonce
        )

        # get validator signatures and verify sig in worker
        validator_indexes = [i for i in range(len(self.stubs))]
        worker = partial(self.get_signature_worker, "GetEthAnchorSignature",
                         anchor, h)
        approvals = self.pool.map(worker, validator_indexes)

        sigs, validator_indexes = self.extract_signatures(approvals)

        return sigs, validator_indexes

    def get_signature_worker(
        self,
        rpc_service: str,
        request,
        h: bytes,
        index: int
    ) -> Optional[Any]:
        """ Get a validator's (index) signature and verify it"""
        try:
            approval = getattr(self.stubs[index], rpc_service)(request)
        except grpc.RpcError as e:
            print(e)
            return None
        if approval.error:
            print("{}{}".format(self.tab, approval.error))
            return None
        if approval.address != self.config_data['validators'][index]['addr']:
            # check nothing is wrong with validator address
            print("{}Unexpected validator {} address : {}"
                  .format(self.tab, index, approval.address))
            return None
        # validate signature
        if not verify_sig(h, approval.sig, approval.address):
            print("{}Invalid signature from validator {}"
                  .format(self.tab, index))
            return None
        return approval

    def extract_signatures(
        self,
        approvals: List[Any]
    ) -> Tuple[List[str], List[int]]:
        """ Convert signatures to hex string and keep 2/3 of them."""
        sigs, validator_indexes = [], []
        for i, approval in enumerate(approvals):
            if approval is not None:
                # convert to hex string for lua
                sigs.append('0x' + approval.sig.hex())
                # +1 for lua indexes
                validator_indexes.append(i+1)
        total_validators = len(self.config_data['validators'])
        if 3 * len(sigs) < 2 * total_validators:
            raise ValidatorMajorityError()
        # slice 2/3 of total validators
        two_thirds = ((total_validators * 2) // 3
                      + ((total_validators * 2) % 3 > 0))
        return sigs[:two_thirds], validator_indexes[:two_thirds]

    def wait_next_anchor(
        self,
        merged_height: int,
    ) -> int:
        """ Wait until t_anchor has passed after merged height.
        Return the next finalized block after t_anchor to be the next anchor
        """
        best_height = self.web3.eth.blockNumber
        lib = best_height - self.t_final
        # wait for merged_height + t_anchor > lib
        wait = (merged_height + self.t_anchor) - lib + 1
        while wait > 0:
            print("{}{} waiting new anchor time : {}s ..."
                  .format(self.tab, u'\u23F0', wait * self.eth_block_time))
            self.monitor_settings_and_sleep(wait * self.eth_block_time)
            # Wait lib > last merged block height + t_anchor
            best_height = self.web3.eth.blockNumber
            lib = best_height - self.t_final
            wait = (merged_height + self.t_anchor) - lib + 1
        return lib

    def new_anchor(
        self,
        root: str,
        next_anchor_height: int,
        validator_indexes: List[int],
        sigs: List[str],
    ) -> None:
        """Anchor a new root on chain"""
        tx, result = self.hera.call_sc(
            self.aergo_bridge, "newAnchor",
            args=[root, next_anchor_height, validator_indexes, sigs]
        )
        if result.status != herapy.CommitStatus.TX_OK:
            print("{}Anchor on aergo Tx commit failed : {}"
                  .format(self.tab, result))
            return

        result = self.hera.wait_tx_result(tx.tx_hash)
        if result.status != herapy.TxResultStatus.SUCCESS:
            print("{}Anchor failed: already anchored, or invalid "
                  "signature: {}".format(self.tab, result))
        else:
            print("{0}{1} Anchor success,\n{0}{2} wait until next anchor "
                  "time: {3}s..."
                  .format(self.tab, u'\u2693', u'\u23F0',
                          self.t_anchor * self.eth_block_time))

    def run(
        self,
    ) -> None:
        """ Gathers signatures from validators, verifies them, and if 2/3 majority
        is acquired, set the new anchored root in aergo_bridge.
        """
        while True:  # anchor a new root
            # Get last merge information
            status = self.hera.query_sc_state(self.aergo_bridge,
                                              ["_sv__anchorHeight",
                                               "_sv__anchorRoot",
                                               "_sv__nonce",
                                               "_sv__tAnchor",
                                               "_sv__tFinal"
                                               ])
            height_from, root_from, nonce_to, t_anchor, t_final = \
                [proof.value for proof in status.var_proofs]
            merged_height_from = int(height_from)
            nonce_to = int(nonce_to)
            self.t_anchor = int(t_anchor)
            self.t_final = int(t_final)

            print("\n{0}| Last anchor from Ethereum:\n"
                  "{0}| --------------------------\n"
                  "{0}| height: {1}\n"
                  "{0}| contract trie root: 0x{2}...\n"
                  "{0}| current update nonce: {3}\n"
                  .format(self.tab, merged_height_from,
                          root_from.decode('utf-8')[1:20], nonce_to))

            # Wait for the next anchor time
            next_anchor_height = self.wait_next_anchor(merged_height_from)
            # Get root of next anchor to broadcast
            state = self.web3.eth.getProof(self.eth_bridge, [],
                                           next_anchor_height)
            root = state.storageHash.hex()[2:]
            if len(root) == 0:
                print("{}waiting deployment finalization..."
                      .format(self.tab))
                time.sleep(5)
                continue

            print("{}anchoring new Ethereum root :'0x{}...'"
                  .format(self.tab, root[:17]))
            print("{}{} Gathering signatures from validators ..."
                  .format(self.tab, u'\U0001f58b'))

            nonce_to = int(self.hera.query_sc_state(
                self.aergo_bridge, ["_sv__nonce"]
            ).var_proofs[0].value)

            try:
                sigs, validator_indexes = self.get_anchor_signatures(
                        root, next_anchor_height, nonce_to
                    )
            except ValidatorMajorityError:
                print("{0}Failed to gather 2/3 validators signatures,\n"
                      "{0}{1} waiting for next anchor..."
                      .format(self.tab, u'\u23F0'))
                self.monitor_settings_and_sleep(
                    self.t_anchor * self.eth_block_time)
                continue

            # don't broadcast if somebody else already did
            last_merge = self.hera.query_sc_state(self.aergo_bridge,
                                                  ["_sv__anchorHeight"])
            merged_height = int(last_merge.var_proofs[0].value)
            if merged_height + self.t_anchor >= next_anchor_height:
                print("{}Not yet anchor time "
                      "or another proposer already anchored".format(self.tab))
                wait = merged_height + self.t_anchor - next_anchor_height
                self.monitor_settings_and_sleep(wait * self.eth_block_time)
                continue

            # Broadcast finalised merge block
            self.new_anchor(root, next_anchor_height, validator_indexes, sigs)
            self.monitor_settings_and_sleep(
                self.t_anchor * self.eth_block_time)

    def monitor_settings_and_sleep(self, sleeping_time):
        """While sleeping, periodicaly check changes to the config
        file and update settings if necessary. If another
        proposer updated settings it doesnt matter, validators will
        just not give signatures.

        """
        if self.auto_update:
            start = time.time()
            self.monitor_settings()
            while time.time()-start < sleeping_time-10:
                # check the config file every 10 seconds
                time.sleep(10)
                self.monitor_settings()
            remaining = sleeping_time - (time.time() - start)
            if remaining > 0:
                time.sleep(remaining)
        else:
            time.sleep(sleeping_time)

    def monitor_settings(self):
        """Check if a modification of bridge settings is requested by seeing
        if the config file has been changed and try to update the bridge
        contract (gather 2/3 validators signatures).

        """
        config_data = self.load_config_data()
        validators = query_aergo_validators(self.hera, self.aergo_bridge)
        t_anchor, t_final = query_aergo_tempo(self.hera, self.aergo_bridge)
        unfreeze_fee = query_unfreeze_fee(self.hera, self.aergo_bridge)
        config_validators = [val['addr']
                             for val in config_data['validators']]
        if validators != config_validators:
            print('{}Validator set update requested'.format(self.tab))
            if self.update_validators(config_validators):
                self.config_data = config_data
                self.update_validator_connections()
        config_t_anchor = (config_data['networks'][self.aergo_net]['bridges']
                           [self.eth_net]['t_anchor'])
        if t_anchor != config_t_anchor:
            print('{}Anchoring periode update requested'.format(self.tab))
            self.update_t_anchor(config_t_anchor)
        config_t_final = (config_data['networks'][self.aergo_net]['bridges']
                          [self.eth_net]['t_final'])
        if t_final != config_t_final:
            print('{}Finality update requested'.format(self.tab))
            self.update_t_final(config_t_final)
        config_unfreeze_fee = (config_data['networks'][self.aergo_net]
                               ['bridges'][self.eth_net]['unfreeze_fee'])
        if unfreeze_fee != config_unfreeze_fee:
            print('{}Unfreeze fee update requested'.format(self.tab))
            self.update_unfreeze_fee(config_unfreeze_fee)

    def update_validator_connections(self):
        """Update connections to validators after a successful update
        of bridge validators with the validators in the config file.

        """
        self.channels = []
        self.stubs = []
        for validator in self.config_data['validators']:
            ip = validator['ip']
            channel = grpc.insecure_channel(ip)
            stub = BridgeOperatorStub(channel)
            self.channels.append(channel)
            self.stubs.append(stub)

        self.pool = Pool(len(self.stubs))

    def update_validators(self, new_validators):
        """Try to update the validator set with the one in the config file."""
        try:
            sigs, validator_indexes = self.get_new_validators_signatures(
                new_validators)
        except ValidatorMajorityError:
            print("{0}Failed to gather 2/3 validators signatures"
                  .format(self.tab))
            return False
        # broadcast transaction
        return self.set_validators(new_validators, validator_indexes, sigs)

    def set_validators(self, new_validators, validator_indexes, sigs):
        """Update validators on chain"""
        tx, result = self.hera.call_sc(
            self.aergo_bridge, "validatorsUpdate",
            args=[new_validators, validator_indexes, sigs]
        )
        if result.status != herapy.CommitStatus.TX_OK:
            print("{}Set new validators Tx commit failed : {}"
                  .format(self.tab, result))
            return False

        result = self.hera.wait_tx_result(tx.tx_hash)
        if result.status != herapy.TxResultStatus.SUCCESS:
            print("{}Set new validators failed : nonce already used, or "
                  "invalid signature: {}".format(self.tab, result))
            return False
        else:
            print("{}{} New validators update success"
                  .format(self.tab, u'\U0001f58b'))
        return True

    def get_new_validators_signatures(self, validators):
        """Request approvals of validators for the new validator set."""
        nonce = int(
            self.hera.query_sc_state(
                self.aergo_bridge, ["_sv__nonce"]).var_proofs[0].value
        )
        new_validators_msg = NewValidators(
            validators=validators, destination_nonce=nonce)
        data = ""
        for val in validators:
            data += val
        data += str(nonce) + self.aergo_id + "V"
        data_bytes = bytes(data, 'utf-8')
        h = hashlib.sha256(data_bytes).digest()
        # get validator signatures and verify sig in worker
        validator_indexes = [i for i in range(len(self.stubs))]
        worker = partial(
            self.get_signature_worker, "GetEthValidatorsSignature",
            new_validators_msg, h
        )
        approvals = self.pool.map(worker, validator_indexes)
        sigs, validator_indexes = self.extract_signatures(approvals)
        return sigs, validator_indexes

    def update_t_anchor(self, t_anchor):
        """Try to update the anchoring periode registered in the bridge
        contract.

        """
        try:
            sigs, validator_indexes = self.get_tempo_signatures(
                t_anchor, "GetEthTAnchorSignature", "A")
        except ValidatorMajorityError:
            print("{0}Failed to gather 2/3 validators signatures"
                  .format(self.tab))
            return
        # broadcast transaction
        self.set_single_param(t_anchor, validator_indexes, sigs,
                              "tAnchorUpdate", "\u231B")

    def set_single_param(
        self,
        num,
        validator_indexes,
        sigs,
        contract_function,
        emoticon
    ) -> bool:
        """Call contract_function with num"""
        tx, result = self.hera.call_sc(
            self.aergo_bridge, contract_function,
            args=[num, validator_indexes, sigs]
        )
        if result.status != herapy.CommitStatus.TX_OK:
            print("{}Set new validators Tx commit failed : {}"
                  .format(self.tab, result))
            return False

        result = self.hera.wait_tx_result(tx.tx_hash)
        if result.status != herapy.TxResultStatus.SUCCESS:
            print("{}Set {} failed: nonce already used, or invalid "
                  "signature: {}".format(self.tab, contract_function, result))
            return False
        else:
            print("{}{} {} success"
                  .format(self.tab, emoticon, contract_function))
        return True

    def update_t_final(self, t_final):
        """Try to update the anchoring periode registered in the bridge
        contract.

        """
        try:
            sigs, validator_indexes = self.get_tempo_signatures(
                t_final, "GetEthTFinalSignature", "F")
        except ValidatorMajorityError:
            print("{0}Failed to gather 2/3 validators signatures"
                  .format(self.tab))
            return
        # broadcast transaction
        self.set_single_param(t_final, validator_indexes, sigs, "tFinalUpdate",
                              "\u231B")

    def get_tempo_signatures(self, tempo, rpc_service, tempo_id):
        """Request approvals of validators for the new t_anchor or t_final."""
        nonce = int(
            self.hera.query_sc_state(
                self.aergo_bridge, ["_sv__nonce"]).var_proofs[0].value
        )
        new_tempo_msg = NewTempo(tempo=tempo, destination_nonce=nonce)
        msg = bytes(
            str(tempo) + str(nonce) + self.aergo_id + tempo_id,
            'utf-8'
        )
        h = hashlib.sha256(msg).digest()
        validator_indexes = [i for i in range(len(self.stubs))]
        worker = partial(
            self.get_signature_worker, rpc_service,
            new_tempo_msg, h
        )
        approvals = self.pool.map(worker, validator_indexes)
        sigs, validator_indexes = self.extract_signatures(approvals)
        return sigs, validator_indexes

    def update_unfreeze_fee(self, fee):
        """Try to update the anchoring periode registered in the bridge
        contract.

        """
        try:
            sigs, validator_indexes = self.get_unfreeze_fee_signatures(
                fee)
        except ValidatorMajorityError:
            print("{0}Failed to gather 2/3 validators signatures"
                  .format(self.tab))
            return
        # broadcast transaction
        self.set_single_param({'_bignum': str(fee)}, validator_indexes, sigs,
                              "unfreezeFeeUpdate", "\U0001f4a7")

    def get_unfreeze_fee_signatures(self, fee):
        """Request approvals of validators for the new t_anchor or t_final."""
        nonce = int(
            self.hera.query_sc_state(
                self.aergo_bridge, ["_sv__nonce"]).var_proofs[0].value
        )
        new_fee_msg = NewUnfreezeFee(fee=fee, destination_nonce=nonce)
        msg = bytes(
            str(fee) + str(nonce) + self.aergo_id + "UF",
            'utf-8'
        )
        h = hashlib.sha256(msg).digest()
        validator_indexes = [i for i in range(len(self.stubs))]
        worker = partial(
            self.get_signature_worker, "GetAergoUnfreezeFeeSignature",
            new_fee_msg, h
        )
        approvals = self.pool.map(worker, validator_indexes)
        sigs, validator_indexes = self.extract_signatures(approvals)
        return sigs, validator_indexes

    def load_config_data(self) -> Dict:
        with open(self.config_file_path, "r") as f:
            config_data = json.load(f)
        return config_data


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Start a proposer on Ethereum and Aergo.')
    # Add arguments
    parser.add_argument(
        '-c', '--config_file_path', type=str, help='Path to config.json',
        required=True)
    parser.add_argument(
        '-a', '--aergo', type=str, help='Name of Aergo network in config file',
        required=True)
    parser.add_argument(
        '-e', '--eth', type=str, help='Name of Ethereum network in config file',
        required=True)
    parser.add_argument(
        '--eth_block_time', type=int, help='Average Ethereum block time',
        required=True)
    parser.add_argument(
        '--privkey_name', type=str, help='Name of account in config file '
        'to sign anchors', required=False)
    parser.add_argument(
        '--auto_update', dest='auto_update', action='store_true',
        help='Update bridge contract when settings change in config file')
    parser.add_argument(
        '--aergo_gas_price', type=int,
        help='Gas price to use in transactions', required=False)
    parser.set_defaults(auto_update=False)
    parser.set_defaults(aergo_gas_price=None)
    args = parser.parse_args()

    proposer = AergoProposerClient(
        args.config_file_path, args.aergo, args.eth, args.eth_block_time,
        privkey_name=args.privkey_name, auto_update=args.auto_update,
        aergo_gas_price=args.aergo_gas_price
    )
    proposer.run()
