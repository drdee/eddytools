from sqlalchemy.engine import Engine, Connection, Transaction
from sqlalchemy.orm import scoped_session, sessionmaker, Session
from sqlalchemy.schema import Table, MetaData, Column
from sqlalchemy.sql import and_, select, or_, insert
from eddytools.casenotions import get_all_classes
from eddytools.events.encoding import Candidate
from eddytools.events.activity_identifier_discovery import ActivityIdentifierDiscoverer
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
                               model='default',
                               model_path=None) -> (list, ActivityIdentifierDiscoverer):

    aid = ActivityIdentifierDiscoverer(engine=mm_engine, meta=mm_meta, model=model, model_path=model_path)

    timestamp_attrs = aid.get_timestamp_attributes(classes=classes)

    # timestamp_attrs = []
    #
    # tb_class: Table = mm_meta.tables['class'].alias('cl')
    # tb_att_n: Table = mm_meta.tables['attribute_name'].alias('at')
    # c_att_type: Column = tb_att_n.columns['type']
    # c_att_name: Column = tb_att_n.columns['name']
    # c_att_id: Column = tb_att_n.columns['id']
    # c_cl_name: Column = tb_class.columns['name']
    #
    # class_names = []
    # if not classes:
    #     cls = get_all_classes(mm_engine, mm_meta)
    #     class_names = [c['name'] for c in cls]
    # else:
    #     class_names = classes
    #
    # query = select([c_att_id]).\
    #     select_from(tb_class.join(tb_att_n)).\
    #     where(and_(c_att_type == 'timestamp',
    #                c_cl_name.in_(class_names)))
    # ts_fields = mm_engine.execute(query)
    # for ts in ts_fields:
    #     timestamp_attrs.append(ts[0])

    # print(timestamp_attrs)

    candidates = aid.generate_candidates(timestamp_attrs=timestamp_attrs, candidate_types='in_table')

    if dump_dir:
        aid.save_candidates(candidates, '{}/candidates.json'.format(dump_dir))

    feature_values = aid.compute_features(candidates, verbose=True)

    if dump_dir:
        aid.save_features(feature_values, '{}/feature_values.json'.format(dump_dir))

    if model:
        predicted = aid.predict(feature_values)
        if dump_dir:
            json.dump(predicted, open('{}/predicted_candidates.json'.format(dump_dir), mode='wt'))
    else:
        predicted = [1 for c in candidates]

    return predicted, candidates, aid


def train_model(mm_engine: Engine, mm_meta: MetaData, y_true_path: str,
                classes: list=None, model_output: str=None):

    aid = ActivityIdentifierDiscoverer(engine=mm_engine, meta=mm_meta,
                                       model=None)
    timestamp_attrs = aid.get_timestamp_attributes(classes=classes)

    candidates = aid.generate_candidates(timestamp_attrs=timestamp_attrs, candidate_types='in_table')

    X = aid.compute_features(candidates, verbose=1)
    y_true = aid.load_y_true(candidates, y_true_path=y_true_path)

    class_weight = compute_class_weight('balanced', [0, 1], y_true)
    classifier = make_sklearn_pipeline(XGBClassifier(max_depth=3, n_estimators=20, random_state=1,
                                                     scale_pos_weight=class_weight[1]))

    aid.set_model(classifier)

    aid.train_model(X, y_true)

    y_pred = aid.predict(X)

    scores = aid.score(y_true, y_pred)

    pprint(scores)

    if model_output:
        with open(model_output, mode='wb') as f:
            pickle.dump(classifier, f)
    return classifier


def ts_to_millis(ts: str):
    d: datetime = dateparser.parse(ts)
    return int(d.timestamp() * 1000)


def compute_events(mm_engine: Engine, mm_meta: MetaData, event_definitions: List[Candidate]):

    DBSession: Session = scoped_session(sessionmaker())

    DBSession.remove()
    DBSession.configure(bind=mm_engine, autoflush=False, expire_on_commit=False, autocommit=False)

    conn = DBSession.connection()

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

                tb_etov = mm_meta.tables['event_to_object_version']
                tb_ai = mm_meta.tables['activity_instance']
                tb_act = mm_meta.tables['activity']
                tb_ev = mm_meta.tables['event']

                num_objs = conn.execute(query.count()).scalar()
                res = conn.execute(query)

                trans: Transaction = conn.begin()

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
                            act_id = int(conn.execute(query).lastrowid)
                            map_act[an_v] = act_id

                        query = tb_ai.insert().values(activity_id=act_id)
                        ai_id = int(conn.execute(query).lastrowid)

                        query = tb_ev.insert().values(activity_instance_id=ai_id,
                                                      timestamp=ts_to_millis(ts_v))

                        ev_id = int(conn.execute(query).lastrowid)

                        query = tb_etov.insert().values(event_id=ev_id,
                                                        object_version_id=ov_id)
                        conn.execute(query)

                        i += 1
                        if i > 1000:
                            trans.commit()
                            i = 0

                    trans.commit()
                    trans.close()
                    trans = conn.begin()

                except Exception as err:
                    trans.rollback()
                    raise(err)

            else:
                raise(Exception('No query for: {}'.format(edc)))

        else:
            # Without a timestamp attribute we cannot create events
            raise(Exception('Without a timestamp attribute we cannot create events: {}'.format(edc)))

    DBSession.commit()
