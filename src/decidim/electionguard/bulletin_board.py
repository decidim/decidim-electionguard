from collections import defaultdict
from typing import Dict, NoReturn, Optional, Set, Literal, Union, Tuple
from electionguard.ballot import CiphertextBallot, from_ciphertext_ballot, BallotBoxState
from electionguard.ballot_validator import ballot_is_valid_for_election
from electionguard.decrypt_with_shares import decrypt_selection_with_decryption_shares
from electionguard.elgamal import elgamal_combine_public_keys
from electionguard.group import ElementModP
from electionguard.key_ceremony import PublicKeySet
from electionguard.tally import CiphertextTally, tally_ballot, PlaintextTallyContest, PlaintextTallySelection
from electionguard.types import CONTEST_ID, GUARDIAN_ID, SELECTION_ID
from electionguard.utils import get_optional
from .common import Content, Context, ElectionStep, Wrapper
from .messages import TrusteePartialKeys, TrusteeVerification, JointElectionKey, TrusteeShare
from .utils import InvalidBallot, pair_with_object_id, serialize, serialize_as_dict, deserialize


class BulletinBoardContext(Context):
    public_keys: Dict[GUARDIAN_ID, ElementModP]
    tally: CiphertextTally
    shares: Dict[GUARDIAN_ID, Dict]

    def __init__(self):
        self.public_keys = {}
        self.has_joint_key = False
        self.shares = {}


class ProcessCreateElection(ElectionStep):
    message_type = 'create_election'

    def process_message(self, message_type: Literal['create_election'],
                        message: dict, context: BulletinBoardContext) -> Tuple[None, ElectionStep]:
        context.build_election(message)
        return None, ProcessStartKeyCeremony()


class ProcessStartKeyCeremony(ElectionStep):
    message_type = 'start_key_ceremony'

    def process_message(self, message_type: Literal['start_key_ceremony'],
                        _message: Content, context: BulletinBoardContext) -> Tuple[None, ElectionStep]:
        return None, ProcessTrusteeElectionKeys()


class ProcessTrusteeElectionKeys(ElectionStep):
    message_type = 'key_ceremony.trustee_election_keys'

    def process_message(self, _message_type: Literal['key_ceremony.trustee_election_keys'],
                        message: Content, context: BulletinBoardContext) -> Tuple[None, Optional[ElectionStep]]:
        content = deserialize(message['content'], PublicKeySet)
        guardian_id = content.owner_id
        guardian_public_key = content.election_public_key
        context.public_keys[guardian_id] = guardian_public_key
        # TO-DO: verify keys?

        if len(context.public_keys) == context.number_of_guardians:
            return None, ProcessTrusteeElectionPartialKeys()
        else:
            return None, None


class ProcessTrusteeElectionPartialKeys(ElectionStep):
    message_type = 'key_ceremony.trustee_partial_election_keys'

    partial_keys_received: Set[GUARDIAN_ID]

    def setup(self):
        self.partial_keys_received = set()

    def process_message(self, _message_type: Literal['key_ceremony.trustee_partial_election_keys'],
                        message: Content, context: BulletinBoardContext) -> Tuple[None, Optional[ElectionStep]]:
        content = deserialize(message['content'], TrusteePartialKeys)
        self.partial_keys_received.add(content.guardian_id)
        # TO-DO: verify partial keys?

        if len(self.partial_keys_received) == context.number_of_guardians:
            return None, ProcessTrusteeVerification()
        else:
            return None, None


class ProcessTrusteeVerification(ElectionStep):
    message_type = 'key_ceremony.trustee_verification'

    verifications_received: Set[GUARDIAN_ID]

    def setup(self):
        self.verifications_received = set()

    def process_message(self, message_type: Literal['key_ceremony.trustee_verification'],
                        message: Content, context: BulletinBoardContext) -> Tuple[Optional[Content], Optional[ElectionStep]]:
        content = deserialize(message['content'], TrusteeVerification)
        self.verifications_received.add(content.guardian_id)
        # TO-DO: check verifications?

        if len(self.verifications_received) < context.number_of_guardians:
            return None, None

        joint_key = elgamal_combine_public_keys(context.public_keys.values())
        context.election_builder.set_public_key(get_optional(joint_key))
        context.election_metadata, context.election_context = get_optional(
            context.election_builder.build()
        )
        return {'message_type': 'end_key_ceremony',
                'content': serialize(JointElectionKey(joint_key=joint_key))
                }, ProcessStartVote()


class ProcessStartVote(ElectionStep):
    message_type = 'start_vote'

    def process_message(self, message_type: Literal['start_vote'],
                        message: dict, context: BulletinBoardContext) -> Tuple[None, ElectionStep]:
        return None, ProcessCastVote()


class ProcessCastVote(ElectionStep):
    def skip_message(self, message_type: str):
        return message_type != 'vote.cast' and message_type != 'end_vote'

    def process_message(self, message_type: Literal['vote.cast', 'end_vote'],
                        message: Union[Content, dict],
                        context: BulletinBoardContext) -> Union[NoReturn, Tuple[None, Optional[ElectionStep]]]:
        if message_type == 'end_vote':
            context.tally = CiphertextTally(
                'election-results', context.election_metadata, context.election_context)
            return None, ProcessStartTally()

        ballot = deserialize(message['content'], CiphertextBallot)
        if not ballot_is_valid_for_election(ballot, context.election_metadata, context.election_context):
            raise InvalidBallot()
        else:
            return None, None


class ProcessStartTally(ElectionStep):
    message_type = 'start_tally'

    def process_message(self, message_type: Literal['start_tally'],
                        message: Content, context: BulletinBoardContext) -> Tuple[None, ElectionStep]:
        return None, ProcessTrusteeShare()


class ProcessTrusteeShare(ElectionStep):
    message_type = 'tally.trustee_share'

    def process_message(self, message_type: str,
                        message: Content, context: BulletinBoardContext) -> Optional[Content]:
        content = deserialize(message['content'], TrusteeShare)
        context.shares[content.guardian_id] = content

        if len(context.shares) < context.number_of_guardians:
            return None, None

        tally_shares = self._prepare_shares_for_decryption(context.shares)
        results: Dict[CONTEST_ID, PlaintextTallyContest] = {}

        for contest in context.tally.cast.values():
            selections: Dict[SELECTION_ID, PlaintextTallySelection] = dict(
                pair_with_object_id(decrypt_selection_with_decryption_shares(
                    selection,
                    tally_shares[selection.object_id],
                    context.election_context.crypto_extended_base_hash
                ))
                for selection in contest.tally_selections.values()
            )

            results[contest.object_id] = serialize_as_dict(
                PlaintextTallyContest(contest.object_id, selections)
            )

        return {'message_type': 'end_tally', 'results': results}, None

    def _prepare_shares_for_decryption(self, tally_shares):
        shares = defaultdict(dict)
        for guardian_id, share in tally_shares.items():
            for question_id, question in share.contests.items():
                for selection_id, selection in question.selections.items():
                    shares[selection_id][guardian_id] = (
                        share.public_key, selection
                    )
        return shares


class BulletinBoard(Wrapper[BulletinBoardContext]):
    def __init__(self) -> None:
        super().__init__(BulletinBoardContext(), ProcessCreateElection())

    def add_ballot(self, ballot: dict):
        ciphertext_ballot = deserialize(ballot, CiphertextBallot)
        # TODO: remove the dependency of multiprocessing
        tally_ballot(from_ciphertext_ballot(ciphertext_ballot,
                                            BallotBoxState.CAST), self.context.tally)

    def get_tally_cast(self) -> Dict:
        return {'message_type': 'tally.cast', 'content': serialize(self.context.tally.cast)}
