import logging
from typing import Tuple

import base  # noqa

import argparse
import asyncio
import random
import traceback
from pathlib import Path

import bittensor as bt
import torch
import wandb
from aiohttp import web
from aiohttp.web_response import Response
from bittensor.btlogging import logger
from image_validator import ImageValidator
from text_validator import TextValidator
from envparse import env

import template
from template import utils
from template.protocol import IsAlive
import sys


moving_average_scores = None
text_vali = None
image_vali = None
embed_vali = None
metagraph = None
wandb_runs = {}
# organic requests are scored, the tasks are stored in this queue
# for later being consumed by `query_synapse` cycle:
organic_scoring_tasks = set()
EXPECTED_ACCESS_KEY = env('EXPECTED_ACCESS_KEY', default='hello')


def get_config() -> bt.config:
    parser = argparse.ArgumentParser()
    parser.add_argument("--netuid", type=int, default=18)
    parser.add_argument('--wandb_off', action='store_false', dest='wandb_on')
    parser.add_argument('--http_port', type=int, default=8000)
    parser.set_defaults(wandb_on=True)
    bt.subtensor.add_args(parser)
    bt.logging.add_args(parser)
    bt.wallet.add_args(parser)
    config = bt.config(parser)
    _args = parser.parse_args()
    full_path = Path(
        f"{config.logging.logging_dir}/{config.wallet.name}/{config.wallet.hotkey}/netuid{config.netuid}/validator"
    ).expanduser()
    config.full_path = str(full_path)
    full_path.mkdir(parents=True, exist_ok=True)
    return config


def init_wandb(config, my_uid, wallet: bt.wallet):
    if not config.wandb_on:
        return

    run_name = f'validator-{my_uid}-{template.__version__}'
    config.uid = my_uid
    config.hotkey = wallet.hotkey.ss58_address
    config.run_name = run_name
    config.version = template.__version__
    config.type = 'validator'

    # Initialize the wandb run for the single project
    run = wandb.init(
        name=run_name,
        project=template.PROJECT_NAME,
        entity='cortex-t',
        config=config,
        dir=config.full_path,
        reinit=True
    )

    # Sign the run to ensure it's from the correct hotkey
    signature = wallet.hotkey.sign(run.id.encode()).hex()
    config.signature = signature
    wandb.config.update(config, allow_val_change=True)

    bt.logging.success(f"Started wandb run for project '{template.PROJECT_NAME}'")


def initialize_components(config: bt.config):
    global metagraph
    bt.logging(config=config, logging_dir=config.full_path)
    bt.logging.info(f"Running validator for subnet: {config.netuid} on network: {config.subtensor.chain_endpoint}")
    wallet = bt.wallet(config=config)
    subtensor = bt.subtensor(config=config)
    metagraph = subtensor.metagraph(config.netuid)
    dendrite = bt.dendrite(wallet=wallet)
    my_uid = metagraph.hotkeys.index(wallet.hotkey.ss58_address)
    if wallet.hotkey.ss58_address not in metagraph.hotkeys:
        bt.logging.error(
            f"Your validator: {wallet} is not registered to chain connection: "
            f"{subtensor}. Run btcli register --netuid 18 and try again."
        )
        sys.exit()

    return wallet, subtensor, dendrite, my_uid


def initialize_validators(vali_config):
    global text_vali, image_vali, embed_vali

    text_vali = TextValidator(**vali_config)
    image_vali = ImageValidator(**vali_config)
    # embed_vali = EmbeddingsValidator(**vali_config)
    bt.logging.info("initialized_validators")


async def check_uid(dendrite, axon, uid):
    """Asynchronously check if a UID is available."""
    try:
        response = await dendrite(axon, IsAlive(), deserialize=False, timeout=4)
        if response.is_success:
            bt.logging.trace(f"UID {uid} is active")
            return axon  # Return the axon info instead of the UID

        bt.logging.trace(f"UID {uid} is not active")
        return None

    except Exception as e:
        bt.logging.error(f"Error checking UID {uid}: {e}\n{traceback.format_exc()}")
        return None

async def get_available_uids(dendrite, metagraph):
    """Get a dictionary of available UIDs and their axons asynchronously."""
    tasks = {uid.item(): check_uid(dendrite, metagraph.axons[uid.item()], uid.item()) for uid in metagraph.uids}
    results = await asyncio.gather(*tasks.values())

    # Create a dictionary of UID to axon info for active UIDs
    available_uids = {uid: axon_info for uid, axon_info in zip(tasks.keys(), results) if axon_info is not None}

    return available_uids


def set_weights(scores, config, subtensor, wallet, metagraph):
    global moving_average_scores
    # alpha of .3 means that each new score replaces 30% of the weight of the previous weights
    alpha = .3
    if moving_average_scores is None:
        moving_average_scores = scores.clone()

    # Update the moving average scores
    moving_average_scores = alpha * scores + (1 - alpha) * moving_average_scores
    bt.logging.info(f"Updated moving average of weights: {moving_average_scores}")
    subtensor.set_weights(netuid=config.netuid, wallet=wallet, uids=metagraph.uids, weights=moving_average_scores, wait_for_inclusion=False)
    bt.logging.success("Successfully set weights.")


def update_weights(total_scores, steps_passed, config, subtensor, wallet, metagraph):
    """ Update weights based on total scores, using min-max normalization for display. """
    avg_scores = total_scores / (steps_passed + 1)

    # Normalize avg_scores to a range of 0 to 1
    min_score = torch.min(avg_scores)
    max_score = torch.max(avg_scores)

    if max_score - min_score != 0:
        normalized_scores = (avg_scores - min_score) / (max_score - min_score)
    else:
        normalized_scores = torch.zeros_like(avg_scores)

    bt.logging.info(f"normalized_scores = {normalized_scores}")
    # We can't set weights with normalized scores because that disrupts the weighting assigned to each validator class
    # Weights get normalized anyways in weight_utils
    set_weights(avg_scores, config, subtensor, wallet, metagraph)


async def process_modality(config, selected_validator, available_uids, metagraph):
    uid_list = list(available_uids.keys())
    random.shuffle(uid_list)
    bt.logging.info(f"starting {selected_validator.__class__.__name__} get_and_score for {uid_list}")
    scores, uid_scores_dict, wandb_data = await selected_validator.get_and_score(uid_list, metagraph)
    if config.wandb_on:
        wandb.log(wandb_data)
        bt.logging.success("wandb_log successful")
    return scores, uid_scores_dict


class TotalScores:
    def __init__(self, len_):
        self.tensor = torch.zeros(len_)


async def query_synapse(dendrite, subtensor, config, wallet):
    global metagraph
    total_scores = TotalScores(len(metagraph.hotkeys))
    iterations_per_set_weights = 12

    async def consume_organic_scoring():
        while True:
            try:
                if organic_scoring_tasks:
                    completed, _ = await asyncio.wait(organic_scoring_tasks, timeout=1,
                                                      return_when=asyncio.FIRST_COMPLETED)
                    for task in completed:
                        if task.exception():
                            logger.error(
                                f'Encountered in {text_vali.score_responses.__name__} task:\n'
                                f'{"".join(traceback.format_exception(task.exception()))}'
                            )
                        else:
                            success, data = task.result()
                            if not success:
                                continue
                            total_scores.tensor += data[0]
                    organic_scoring_tasks.difference_update(completed)
                else:
                    await asyncio.sleep(1)
            except Exception as e:
                logger.error(f'Encountered in {consume_organic_scoring.__name__} loop:\n{traceback.format_exc()}')
                await asyncio.sleep(10)

    asyncio.create_task(consume_organic_scoring())

    steps_passed = 0

    while True:
        try:
            metagraph = subtensor.metagraph(config.netuid)

            available_uids = await get_available_uids(dendrite, metagraph)
            selected_validator = text_vali if steps_passed % 5 in (0, 1, 2) else image_vali
            scores, _uid_scores_dict = await process_modality(config, selected_validator, available_uids, metagraph)
            total_scores.tensor += scores

            iterations_until_update = iterations_per_set_weights - ((steps_passed + 1) % iterations_per_set_weights)
            bt.logging.info(f"Updating weights in {iterations_until_update} iterations.")

            if iterations_until_update == 1:
                update_weights(total_scores.tensor, steps_passed, config, subtensor, wallet, metagraph)

            steps_passed += 1
            await asyncio.sleep(0.5)

        except Exception:
            logger.error(f'Encountered in {query_synapse.__name__} loop:\n{traceback.format_exc()}')
            await asyncio.sleep(10)


async def wait_for_coro_with_limit(coro, timeout: int) -> Tuple[bool, object]:
    try:
        result = await asyncio.wait_for(coro, timeout)
    except asyncio.TimeoutError:
        logger.error('scoring task timed out')
        return False, None
    return True, result


async def process_text_validator(request: web.Request):
    # Check access key
    access_key = request.headers.get("access-key")
    if access_key != EXPECTED_ACCESS_KEY:
        return Response(status=401, reason="Invalid access key")

    try:
        messages_dict = {int(k): [{'role': 'user', 'content': v}] for k, v in (await request.json()).items()}
    except ValueError:
        return Response(status=400)

    response = web.StreamResponse()
    await response.prepare(request)

    uid_to_response = dict.fromkeys(messages_dict, "")
    try:
        async for uid, content in text_vali.organic(metagraph, messages_dict):
            uid_to_response[uid] += content
            await response.write(content.encode())
        organic_scoring_tasks.add(asyncio.create_task(
            wait_for_coro_with_limit(
                text_vali.score_responses(
                    query_responses=list(uid_to_response.items()),
                    uid_to_question=messages_dict,
                    metagraph=metagraph,
                ),
                60
            )
        ))
    except Exception:
        logger.error(f'Encountered in {process_text_validator.__name__}:\n{traceback.format_exc()}')
        await response.write(b'<<internal error>>')

    return response

aio_app = web.Application()
aio_app.add_routes([web.post('/text-validator/', process_text_validator)])


def main() -> None:
    config = get_config()
    wallet, subtensor, dendrite, my_uid = initialize_components(config)
    validator_config = {
        "dendrite": dendrite,
        "config": config,
        "subtensor": subtensor,
        "wallet": wallet
    }
    initialize_validators(validator_config)
    init_wandb(config, my_uid, wallet)
    loop = asyncio.get_event_loop()
    try:
        loop.create_task(query_synapse(dendrite, subtensor, config, wallet))
        web.run_app(aio_app, port=config.http_port, loop=loop)
    except KeyboardInterrupt:
        bt.logging.info("Keyboard interrupt detected. Exiting validator.")
    finally:
        state = utils.get_state()
        utils.save_state_to_file(state)
        if config.wandb_on:
            wandb.finish()


if __name__ == "__main__":
    main()
