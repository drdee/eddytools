from sqlalchemy.engine import Engine, Connection, Transaction
from sqlalchemy.orm import scoped_session, sessionmaker, Session
from sqlalchemy.schema import Table, MetaData, Column
from sqlalchemy.sql import and_, select, or_, insert
from eddytools.casenotions import get_all_classes
from eddytools.events.encoding import Candidate
from eddytools.events.activity_identifier_discovery import ActivityIdentifierDiscoverer,\
    CT_TS_FIELD, CT_IN_TABLE, CT_LOOKUP
from eddytools.events import activity_identifier_feature_functions as evff
from eddytools.events.activity_identifier_predictors import make_sklearn_pipeline
import json
from xgboost.sklearn import XGBClassifier
from sklearn.utils.class_weight import compute_class_weight
from typing import List
import dateparser
from datetime import datetime
from tqdm import tqdm
import pickle
from pprint import pprint


def discover_event_definitions(mm_engine: Engine, mm_meta: MetaData,
                               classes: list=None,
                               dump_dir: str=None,
                               candidate_types=(CT_TS_FIELD, CT_IN_TABLE, CT_LOOKUP),
                               model='default',
                               model_path=None) -> (list, ActivityIdentifierDiscoverer):

    aid = ActivityIdentifierDiscoverer(engine=mm_engine, meta=mm_meta, model=model, model_path=model_path)

    timestamp_attrs = aid.get_timestamp_attributes(classes=classes)

    candidates_ts_field = []
    candidates_in_table = []
    candidates_lookup = []
    predicted_ts_field = []
    predicted_in_table = []
    predicted_lookup = []

    if CT_TS_FIELD in candidate_types:
        candidates_ts_field = aid.generate_candidates(timestamp_attrs=timestamp_attrs, candidate_type=CT_TS_FIELD)
        predicted_ts_field = [1 for c in candidates_ts_field]  # All ts fields can be considered as valid candidates
        if dump_dir:
            aid.save_candidates(candidates_ts_field, '{}/candidates_ts_field.json'.format(dump_dir))
            json.dump(predicted_ts_field, open('{}/predicted_candidates_ts_field.json'.format(dump_dir), mode='wt'))

    if CT_IN_TABLE in candidate_types:
        candidates_in_table = aid.generate_candidates(timestamp_attrs=timestamp_attrs, candidate_type=CT_IN_TABLE)
        if dump_dir:
            aid.save_candidates(candidates_in_table, '{}/candidates_in_table.json'.format(dump_dir))

        feature_values_in_table = aid.compute_features(candidates_in_table, verbose=True)
        if dump_dir:
            aid.save_features(feature_values_in_table, '{}/feature_values_in_table.json'.format(dump_dir))

        if model:
            predicted_in_table = aid.predict(feature_values_in_table, candidate_type=CT_IN_TABLE)
            if dump_dir:
                json.dump(predicted_in_table, open('{}/predicted_candidates_in_table.json'.format(dump_dir), mode='wt'))
        else:
            predicted_in_table = [1 for c in candidates_in_table]

    if CT_LOOKUP in candidate_types:
        candidates_lookup = aid.generate_candidates(timestamp_attrs=timestamp_attrs, candidate_type=CT_LOOKUP)
        if dump_dir:
            aid.save_candidates(candidates_lookup, '{}/candidates_lookup.json'.format(dump_dir))

        feature_values_lookup = aid.compute_features(candidates_lookup, verbose=True)
        if dump_dir:

            aid.save_features(feature_values_lookup, '{}/feature_values_lookup.json'.format(dump_dir))

        if model:
            predicted_lookup = aid.predict(feature_values_lookup, candidate_type=CT_LOOKUP)
            if dump_dir:
                json.dump(predicted_lookup, open('{}/predicted_candidates_lookup.json'.format(dump_dir), mode='wt'))
        else:
            predicted_lookup = [1 for c in candidates_lookup]

    return {
        CT_TS_FIELD: {'predicted': predicted_ts_field,
                      'candidates': candidates_ts_field},
        CT_IN_TABLE: {'predicted': predicted_in_table,
                      'candidates': candidates_in_table},
        CT_LOOKUP: {'predicted': predicted_lookup,
                    'candidates': candidates_lookup},
        'aid': aid
    }


def train_model(mm_engine: Engine, mm_meta: MetaData, y_true_path: str,
                classes: list=None, model_output: str=None):

    aid = ActivityIdentifierDiscoverer(engine=mm_engine, meta=mm_meta,
                                       model=None)

    timestamp_attrs = aid.get_timestamp_attributes(classes=classes)

    candidates_ts_field = aid.generate_candidates(timestamp_attrs=timestamp_attrs, candidate_type=CT_TS_FIELD)
    candidates_in_table = aid.generate_candidates(timestamp_attrs=timestamp_attrs, candidate_type=CT_IN_TABLE)
    candidates_lookup = aid.generate_candidates(timestamp_attrs=timestamp_attrs, candidate_type=CT_LOOKUP)

    X_in_table = aid.compute_features(candidates_in_table, verbose=1)
    X_lookup = aid.compute_features(candidates_lookup, verbose=1)

    y_true_in_table = aid.load_y_true(candidates_in_table, y_true_path=y_true_path)
    y_true_lookup = aid.load_y_true(candidates_lookup, y_true_path=y_true_path)

    class_weight_in_table = compute_class_weight('balanced', [0, 1], y_true_in_table)
    class_weight_lookup = compute_class_weight('balanced', [0, 1], y_true_lookup)

    classifier_in_table = make_sklearn_pipeline(XGBClassifier(max_depth=2, n_estimators=10, random_state=1,
                                                              scale_pos_weight=class_weight_in_table[1]))

    classifier_lookup = make_sklearn_pipeline(XGBClassifier(max_depth=2, n_estimators=10, random_state=1,
                                                            scale_pos_weight=class_weight_lookup[1]))

    classifiers = {'in_table': classifier_in_table,
                   'lookup': classifier_lookup}

    aid.set_model(classifiers)

    aid.train_model(X_in_table, y_true_in_table, candidate_type=CT_IN_TABLE)
    aid.train_model(X_lookup, y_true_lookup, candidate_type=CT_LOOKUP)

    y_pred_in_table = aid.predict(X_in_table, candidate_type=CT_IN_TABLE)
    y_pred_lookup = aid.predict(X_lookup, candidate_type=CT_LOOKUP)

    scores_in_table = aid.score(y_true_in_table, y_pred_in_table)

    print('Scores In Table')
    pprint(scores_in_table)

    scores_lookup = aid.score(y_true_lookup, y_pred_lookup)

    print('Scores Lookup')
    pprint(scores_lookup)

    if model_output:
        with open(model_output, mode='wb') as f:
            pickle.dump(classifiers, f)
    return classifiers


def ts_to_millis(ts: str):
    d: datetime = dateparser.parse(ts)
    return int(d.timestamp() * 1000)


def compute_events(mm_engine: Engine, mm_meta: MetaData, event_definitions: List[Candidate],
                   mm_engine_modif: Engine=None, mm_meta_modif: MetaData=None):

    # DBSession: Session = scoped_session(sessionmaker())
    #
    # DBSession.remove()
    # DBSession.configure(bind=mm_engine, autoflush=False, expire_on_commit=False, autocommit=False)
    #

    conn = mm_engine.connect()

    if not mm_engine_modif:
        mm_engine_modif = mm_engine
        mm_meta_modif = mm_meta
        conn_modif = conn
    else:
        conn_modif = mm_engine_modif.connect()

    with conn and conn_modif:

    # conn = DBSession.connection()

        for ed in tqdm(event_definitions, desc='Event definitions'):
            edc = Candidate(timestamp_attribute_id=ed[0],
                            activity_identifier_attribute_id=ed[1],
                            relationship_id=ed[2])
            ts_id = edc.timestamp_attribute_id
            ac_at_id = edc.activity_identifier_attribute_id
            rs_id = edc.relationship_id

            if ts_id:
                query = None
                if ac_at_id:
                    if rs_id:
                        # It is a look-up table
                        tb_ov = mm_meta.tables['object_version']
                        tb_av = mm_meta.tables['attribute_value']
                        tb_rel = mm_meta.tables['relation']

                        tb_ov_ts = tb_ov.alias('OV_TS')
                        tb_ov_an = tb_ov.alias('OV_AN')
                        tb_av_ts = tb_av.alias('TS_AV')
                        tb_av_an = tb_av.alias('AN_AV')

                        query = select([tb_ov_ts.c.id.label('ov_id'),
                                        tb_av_ts.c.value.label('ts_v'),
                                        tb_av_an.c.value.label('an_v')]). \
                            where(and_(tb_ov_ts.c.id == tb_av_ts.c.object_version_id,
                                       tb_av_ts.c.attribute_name_id == ts_id,
                                       tb_ov_an.c.id == tb_av_an.c.object_version_id,
                                       tb_av_an.c.attribute_name_id == ac_at_id,
                                       # or_(and_(tb_ov_ts.c.id == tb_rel.c.source_object_version_id,
                                       #          tb_ov_an.c.id == tb_rel.c.target_object_version_id),
                                       #     and_(tb_ov_ts.c.id == tb_rel.c.target_object_version_id,
                                       #          tb_ov_an.c.id == tb_rel.c.source_object_version_id)),
                                       tb_ov_ts.c.id == tb_rel.c.source_object_version_id,
                                       tb_ov_an.c.id == tb_rel.c.target_object_version_id,
                                       tb_rel.c.relationship_id == rs_id))
                    else:
                        # It is in-table
                        tb_ov = mm_meta.tables['object_version']
                        tb_av = mm_meta.tables['attribute_value']

                        tb_av_ts = tb_av.alias('TS_AV')
                        tb_av_an = tb_av.alias('AN_AV')

                        query = select([tb_ov.c.id.label('ov_id'),
                                        tb_av_ts.c.value.label('ts_v'),
                                        tb_av_an.c.value.label('an_v')]). \
                            where(and_(tb_ov.c.id == tb_av_ts.c.object_version_id,
                                       tb_ov.c.id == tb_av_an.c.object_version_id,
                                       tb_av_ts.c.attribute_name_id == ts_id,
                                       tb_av_an.c.attribute_name_id == ac_at_id))
                else:
                    # It is a column-name event: Create one event for each timestamp
                    # with the column name as activity name
                    tb_ov = mm_meta.tables['object_version']
                    tb_av = mm_meta.tables['attribute_value']
                    tb_an = mm_meta.tables['attribute_name']

                    query = select([tb_ov.c.id.label('ov_id'),
                                    tb_av.c.value.label('ts_v'),
                                    tb_an.c.name.label('an_v')]).\
                        where(and_(tb_ov.c.id == tb_av.c.object_version_id,
                                   tb_av.c.attribute_name_id == ts_id,
                                   tb_an.c.id == ts_id))

                if query is not None:

                    tb_etov = mm_meta_modif.tables['event_to_object_version']
                    tb_ai = mm_meta_modif.tables['activity_instance']
                    tb_act = mm_meta_modif.tables['activity']
                    tb_ev = mm_meta_modif.tables['event']

                    num_objs = conn.execute(query.count()).scalar()
                    res = conn.execute(query)

                    trans: Transaction = conn_modif.begin()
                    # trans: Transaction = conn.begin()

                    map_act = {}

                    try:
                        i = 0
                        for r in tqdm(res, total=num_objs, desc='Events'):
                            ov_id = int(r['ov_id'])
                            ts_v = str(r['ts_v'])
                            an_v = str(r['an_v'])

                            act_id = map_act.get(an_v, None)

                            # Create activities, activity instances, events, and connection to object versions
                            if not act_id:
                                query = tb_act.insert().values(name=an_v)
                                act_id = int(conn_modif.execute(query).lastrowid)
                                map_act[an_v] = act_id

                            query = tb_ai.insert().values(activity_id=act_id)
                            ai_id = int(conn_modif.execute(query).lastrowid)

                            query = tb_ev.insert().values(activity_instance_id=ai_id,
                                                          timestamp=ts_to_millis(ts_v))

                            ev_id = int(conn_modif.execute(query).lastrowid)

                            query = tb_etov.insert().values(event_id=ev_id,
                                                            object_version_id=ov_id)
                            conn_modif.execute(query)

                            i += 1
                            if i > 1000:
                                trans.commit()
                                trans = conn_modif.begin()
                                i = 0

                        trans.commit()
                        trans.close()

                    except Exception as err:
                        trans.rollback()
                        raise(err)

                else:
                    raise(Exception('No query for: {}'.format(edc)))

            else:
                # Without a timestamp attribute we cannot create events
                raise(Exception('Without a timestamp attribute we cannot create events: {}'.format(edc)))

    # DBSession.commit()
