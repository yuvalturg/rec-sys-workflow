from kfp import dsl, compiler
from typing import List, Dict
import os
from kfp import kubernetes
from kfp.dsl import Input, Output, Dataset, Model

IMAGE_TAG = '0.0.24'
# IMAGE_TAG = 'latest'

@dsl.component(
    base_image=f"quay.io/ecosystem-appeng/rec-sys-app:{IMAGE_TAG}")
def generate_candidates(item_input_model: Input[Model], user_input_model: Input[Model], item_df_input: Input[Dataset], user_df_input: Input[Dataset]):
    from feast import FeatureStore
    from feast.data_source import PushMode
    from models.data_util import data_preproccess
    from models.user_tower import UserTower
    from models.item_tower import ItemTower
    import pandas as pd
    import numpy as np
    from datetime import datetime
    import torch
    import subprocess

    result = subprocess.run(
        ["/bin/bash", "-c", "./entry_point.sh", "&&", "ls"],
        capture_output=True,  # Capture stdout and stderr
        text=True,           # Return output as strings (not bytes)
        # check=True           # Raise an error if the command fails
    )
    
    # Print the stdout
    print("Standard Output:")
    print(result.stdout)
    
    # Print the stderr (if any)
    print("Standard Error:")
    print(result.stderr)
    with open('feature_repo/feature_store.yaml', 'r') as file:
        print(file.read())

    store = FeatureStore(repo_path="feature_repo/")

    item_encoder = ItemTower()
    user_encoder = UserTower()
    item_encoder.load_state_dict(torch.load(item_input_model.path))
    user_encoder.load_state_dict(torch.load(user_input_model.path))
    item_encoder.eval()
    user_encoder.eval()
    # load item and user dataframes
    item_df = pd.read_parquet(item_df_input.path)
    user_df = pd.read_parquet(user_df_input.path)

    # Create a new table to be push to the online store
    item_embed_df = item_df[['item_id']].copy()
    user_embed_df = user_df[['user_id']].copy()

    # Encode the items and users
    item_embed_df['embedding'] = item_encoder(**data_preproccess(item_df)).detach().numpy().tolist()
    user_embed_df['embedding'] = user_encoder(**data_preproccess(user_df)).detach().numpy().tolist()

    # Add the currnet timestamp
    item_embed_df['event_timestamp'] = datetime.now()
    user_embed_df['event_timestamp'] = datetime.now()

    # Push the new embedding to the offline and online store
    store.push('item_embed_push_source', item_embed_df, to=PushMode.ONLINE, allow_registry_cache=False)
    store.push('user_embed_push_source', user_embed_df, to=PushMode.ONLINE, allow_registry_cache=False)

    # Materilize the online store
    store.materialize_incremental(datetime.now(), feature_views=['item_embedding', 'user_items', 'item_features'])

    # Calculate user recommendations for each user
    item_embedding_view = 'item_embedding'
    k = 64
    item_recommendation = []
    for user_embed in user_embed_df['embedding']:
        item_recommendation.append(
            store.retrieve_online_documents(
                query=user_embed,
                top_k=k,
                features=[f'{item_embedding_view}:item_id']
            ).to_df()['item_id'].to_list()
        )

    # Pushing the calculated items to the online store
    user_items_df = user_embed_df[['user_id']].copy()
    user_items_df['event_timestamp'] = datetime.now()
    user_items_df['top_k_item_ids'] = item_recommendation

    store.push('user_items_push_source', user_items_df, to=PushMode.ONLINE, allow_registry_cache=False)


@dsl.component(base_image=f"quay.io/ecosystem-appeng/rec-sys-app:{IMAGE_TAG}")
def train_model(item_df_input: Input[Dataset], user_df_input: Input[Dataset], interaction_df_input: Input[Dataset], neg_interaction_df_input:Input[Dataset], item_output_model: Output[Model], user_output_model: Output[Model]):
    from models.user_tower import UserTower
    from models.item_tower import ItemTower
    from models.train_two_tower import train_two_tower
    import pandas as pd
    import torch
    dim = 64

    item_df = pd.read_parquet(item_df_input.path)
    user_df = pd.read_parquet(user_df_input.path)
    interaction_df = pd.read_parquet(interaction_df_input.path)
    neg_interaction_df = pd.read_parquet(neg_interaction_df_input.path)

    item_encoder = ItemTower(dim)
    user_encoder = UserTower(dim)
    train_two_tower(item_encoder, user_encoder, item_df, user_df, interaction_df, neg_interaction_df)

    torch.save(item_encoder.state_dict(), item_output_model.path)
    torch.save(user_encoder.state_dict(), user_output_model.path)
    item_output_model.metadata['framework'] = 'pytorch'
    user_output_model.metadata['framework'] = 'pytorch'

@dsl.component(
    base_image=f"quay.io/ecosystem-appeng/rec-sys-app:{IMAGE_TAG}", packages_to_install=['psycopg2'])
def load_data_from_feast(item_df_output: Output[Dataset], user_df_output: Output[Dataset], interaction_df_output: Output[Dataset], neg_interaction_df_output: Output[Dataset]):
    from feast import FeatureStore
    from datetime import datetime
    import pandas as pd
    import os
    import psycopg2
    from sqlalchemy import create_engine, text
    import subprocess

    result = subprocess.run(
        ["/bin/bash", "-c", "./entry_point.sh", "&&", "ls"],
        capture_output=True,  # Capture stdout and stderr
        text=True,           # Return output as strings (not bytes)
    )
    
    # Print the stdout
    print("Standard Output:")
    print(result.stdout)
    
    # Print the stderr (if any)
    print("Standard Error:")
    print(result.stderr)
    with open('feature_repo/feature_store.yaml', 'r') as file:
        print(file.read())
    store = FeatureStore(repo_path="feature_repo/")
    store.refresh_registry()
    # load feature services
    item_service = store.get_feature_service("item_service")
    user_service = store.get_feature_service("user_service")
    interaction_service = store.get_feature_service("interaction_service")
    neg_interactions_service = store.get_feature_service('neg_interaction_service')

    num_users = 1_000
    n_items = 5_000

    user_ids = list(range(1, num_users+ 1))
    item_ids = list(range(1, n_items+ 1))

    # select which items to use for the training
    item_entity_df = pd.DataFrame.from_dict(
        {
            'item_id': item_ids,
            'event_timestamp': [datetime(2025, 1, 1)] * len(item_ids)
        }
    )
    # select which users to use for the training
    user_entity_df = pd.DataFrame.from_dict(
        {
            'user_id': user_ids,
            'event_timestamp': [datetime(2025, 1, 1)] * len(user_ids)
        }
    )
    # Select which item-user interactions to use for the training
    item_user_interactions_df = pd.read_parquet('./feature_repo/data/interactions_item_user_ids.parquet')
    item_user_neg_interactions_df = pd.read_parquet('./feature_repo/data/neg_interactions_item_user_ids.parquet')
    item_user_interactions_df['event_timestamp'] = datetime(2025, 1, 1)
    item_user_neg_interactions_df['event_timestamp'] = datetime(2025, 1, 1)

    # retrive datasets for training
    item_df = store.get_historical_features(entity_df=item_entity_df, features=item_service).to_df()
    user_df = store.get_historical_features(entity_df=user_entity_df, features=user_service).to_df()
    interaction_df = store.get_historical_features(entity_df=item_user_interactions_df, features=interaction_service).to_df()
    neg_interaction_df = store.get_historical_features(entity_df=item_user_neg_interactions_df, features=neg_interactions_service).to_df()

    uri = os.getenv('uri', None)
    engine = create_engine(uri)

    def table_exists(engine, table_name):
        query = text("SELECT COUNT(*) FROM information_schema.tables WHERE table_name = :table_name")
        with engine.connect() as connection:
            result = connection.execute(query, {"table_name": table_name}).scalar()
            return result > 0

    if table_exists(engine, 'stream_interaction_negetive'):
        query_negative = 'SELECT * FROM stream_interaction_negetive'
        stream_negetive_inter_df = pd.read_sql(query_negative, engine).rename(columns={'timestamp':'event_timestamp'})

        neg_interaction_df = pd.concat([neg_interaction_df, stream_negetive_inter_df], axis=0)

    if table_exists(engine, 'stream_interaction_positive'):
        query_positive = 'SELECT * FROM stream_interaction_positive'
        stream_positive_inter_df = pd.read_sql(query_positive, engine).rename(columns={'timestamp':'event_timestamp'})

        interaction_df = pd.concat([interaction_df, stream_positive_inter_df], axis=0)

    # Pass artifacts
    item_df.to_parquet(item_df_output.path)
    user_df.to_parquet(user_df_output.path)
    interaction_df.to_parquet(interaction_df_output.path)
    neg_interaction_df.to_parquet(neg_interaction_df_output.path)

    item_df_output.metadata['format'] = 'parquet'
    user_df_output.metadata['format'] = 'parquet'
    interaction_df_output.metadata['format'] = 'parquet'
    neg_interaction_df_output.metadata['format'] = 'parquet'


def mount_secret_feast_repository(task):
    kubernetes.use_secret_as_env(
        task=task,
        secret_name=os.getenv('DB_SECRET_NAME', 'cluster-sample-app'),
        secret_key_to_env={'uri': 'uri', 'password': 'DB_PASSWORD'},
    )
    kubernetes.use_secret_as_volume(
        task=task,
        secret_name='feast-feast-edb-rec-sys-registry-tls',
        mount_path='/app/feature_repo/secrets',
    )

@dsl.pipeline(name=os.path.basename(__file__).replace(".py", ""))
def batch_recommendation():

    load_data_task = load_data_from_feast()
    mount_secret_feast_repository(load_data_task)
    # Component configurations
    load_data_task.set_caching_options(False)

    train_model_task = train_model(
        item_df_input=load_data_task.outputs['item_df_output'],
        user_df_input=load_data_task.outputs['user_df_output'],
        interaction_df_input=load_data_task.outputs['interaction_df_output'],
        neg_interaction_df_input=load_data_task.outputs['neg_interaction_df_output'],
    ).after(load_data_task)
    train_model_task.set_caching_options(False)

    generate_candidates_task = generate_candidates(
        item_input_model=train_model_task.outputs['item_output_model'],
        user_input_model=train_model_task.outputs['user_output_model'],
        item_df_input=load_data_task.outputs['item_df_output'],
        user_df_input=load_data_task.outputs['user_df_output'],
    ).after(train_model_task)
    kubernetes.use_secret_as_env(
        task=generate_candidates_task,
        secret_name=os.getenv('DB_SECRET_NAME', 'cluster-sample-app'),
        secret_key_to_env={'uri': 'uri', 'password': 'DB_PASSWORD'},
    )
    kubernetes.use_secret_as_volume(
        task=generate_candidates_task,
        secret_name='feast-feast-edb-rec-sys-registry-tls',
        mount_path='/app/feature_repo/secrets',
    )
    generate_candidates_task.set_caching_options(False)


if __name__ == "__main__":
    compiler.Compiler().compile(
        pipeline_func=batch_recommendation,
        package_path=__file__.replace(".py", ".yaml"),
    )
