import catboost as ctb
import lightgbm as lgb
import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.ensemble import RandomForestClassifier

from ml.models.ml_models.toolbox import BaseMLPredictor


def decision_tree_args(parser):

    decision_tree_parser = parser.add_argument_group("Decision tree-like model hyper parameters")
    decision_tree_parser.add_argument('--n-estimators', type=int, default=200)
    decision_tree_parser.add_argument('--num-iterations', type=int, default=1000)
    decision_tree_parser.add_argument('--n-leaves', type=int, default=32)
    decision_tree_parser.add_argument('--max-bin', type=int, default=255)
    decision_tree_parser.add_argument('--max-depth', type=int, default=5)
    decision_tree_parser.add_argument('--min-data-in-leaf', type=int, default=50)
    decision_tree_parser.add_argument('--reg-alpha', type=float, default=0.5, help='L1 regularization term on weights')
    decision_tree_parser.add_argument('--reg-lambda', type=float, default=0.5, help='L2 regularization term on weights')
    decision_tree_parser.add_argument('--subsample', type=float, default=0.8, help='Sample rate for bagging')
    decision_tree_parser.add_argument('--feature-fraction', type=float, default=0.8, help='Sample rate for bagging')

    return parser


from dataclasses import dataclass
@dataclass
class DecisionTreeConfig:
    n_estimators: int = 200
    num_iterations: int = 1000      # TODO n_iterations
    n_leaves: int = 32
    max_bin: int = 255
    max_depth: int = 5
    min_data_in_leaf: int = 50
    reg_alpha: float = 0.5      # L1 regularization term on weights'
    reg_lambda: float = 0.5     # L2 regularization term on weights
    sabsample: float = 0.8         # TODO sabsample_rate: Sample rate for bagging
    feature_fraction: float = 0.8  # Sample rate for bagging


def get_feature_importance(model_cls, features):
    feature_importances = pd.DataFrame()
    feature_importances['feature'] = features
    feature_importances['importance'] = model_cls.model.feature_importances_
    feature_importances = feature_importances.sort_values(by='importance', ascending=False)
    return feature_importances


class RandomForest(BaseMLPredictor):
    def __init__(self, class_labels, cfg):
        self.model = RandomForestClassifier(n_estimators=cfg['n_estimators'], max_depth=cfg['max_depth'],
                                            random_state=cfg['seed'], verbose=1, class_weight='balanced',
                                            max_samples=cfg['subsample'])
        super(RandomForest, self).__init__(class_labels, cfg)


class XGBoost(BaseMLPredictor):
    def __init__(self, class_labels, cfg):
        self.classify = cfg['task_type'].value == 'classify'
        params = dict(
            learning_rate=cfg['lr'],
            n_estimators=cfg['n_estimators'],
            max_depth=cfg['max_depth'],
            min_child_weight=1,
            min_split_loss=0,
            subsample=cfg['subsample'],
            colsample_bytree=0.8,
            n_jobs=cfg['n_jobs'],
            reg_lambda=cfg['reg_lambda'],
            reg_alpha=cfg['reg_alpha'],
            missing=None,
            random_state=cfg['seed'],
        )
        if cfg['task_type'] == 'classify':
            params['num_class'] = len(class_labels)
            params['objective'] = 'multi:softprob'
            self.model = xgb.XGBClassifier(**params)
        else:
            params['objective'] = 'reg:linear'
            self.model = xgb.XGBRegressor(**params)
        super(XGBoost, self).__init__(class_labels, cfg)

    def fit(self, x, y):
        eval_metric = 'mlogloss' if self.classify else 'rmse'
        self.model.fit(x, y, eval_set=[(x, y)], eval_metric=eval_metric, verbose=False)
        return np.array(self.model.evals_result()['validation_0'][eval_metric]).mean()

    def predict(self, x):
        return self.model.predict(x)


class CatBoost(BaseMLPredictor):
    def __init__(self, class_labels, cfg):
        # TODO visualizationも試す
        self.classify = cfg['task_type'].value == 'classify'

        params = dict(
            iterations=cfg['n_estimators'],
            depth=cfg['max_depth'],
            learning_rate=cfg['lr'],
            random_seed=cfg['seed'],
            has_time=True,
            reg_lambda=cfg['reg_lambda'],
            class_weights=cfg['loss_weight'],
            bootstrap_type='Bernoulli',
            subsample=cfg['subsample'],
            task_type='GPU' if cfg['cuda'] else 'CPU',
        )
        if self.classify:
            params['eval_metric'] = 'Accuracy'
            self.model = ctb.CatBoostClassifier(**params)
        else:
            del params['class_weights']
            self.model = ctb.CatBoostRegressor(**params)
        super(CatBoost, self).__init__(class_labels, cfg)

    def fit(self, x, y):
        self.model.fit(x, y, verbose=False)
        if self.classify:
            return self.model.best_score_['learn']['Accuracy']
        else:
            return self.model.best_score_['learn']['RMSE']

    def predict(self, x):
        return self.model.predict(x).reshape((-1,))


class LightGBM(BaseMLPredictor):
    def __init__(self, class_labels, cfg):
        # TODO visualizationも試す
        self.classify = cfg['task_type'] == 'classify'

        self.params = dict(
            num_leaves=cfg['n_leaves'],
            # n_estimators=cfg['n_estimators'],
            learning_rate=cfg['lr'],
            max_depth=cfg['max_depth'],
            subsample=cfg['subsample'],
            n_jobs=cfg['n_jobs'],
            reg_lambda=cfg['reg_lambda'],
            reg_alpha=cfg['reg_alpha'],
            # class_weight={i: weight for i, weight in enumerate(cfg['loss_weight'])},
            class_weight='balanced',
            missing=None,
            seed=cfg['seed'],
            max_bin=cfg['max_bin'],
            num_iterations=cfg['num_iterations'],
            min_child_samples=cfg['min_data_in_leaf'],
            colsample_bytree=cfg['feature_fraction'],
        )
        if self.classify:
            if len(cfg['class_names']) == 2:
                self.params['metric'] = 'binary_logloss'
                self.params['objective'] = 'binary'
            else:
                self.params['num_class'] = len(cfg['class_names'])
                self.params['metric'] = 'multi_logloss'
            self.model = lgb.LGBMClassifier(**self.params)
        else:
            self.params['objective'] = 'regression'
            self.params['metric'] = 'rmse'
            self.model = lgb.LGBMRegressor(**self.params, num_trees=cfg['n_estimators'])
        super(LightGBM, self).__init__(class_labels, cfg)

    def fit(self, x, y, eval_x=None, eval_y=None):
        eval_set = [(x, y)]
        if isinstance(eval_x, np.ndarray):
            eval_set.append((eval_x, eval_y))

            # return self.model.best_score_['training'][self.params['metric']]
        self.model.fit(x, y, eval_set=eval_set, verbose=50, early_stopping_rounds=20)
        return list(self.model.best_score_.keys())[-1]

    def predict(self, x):
        return self.model.predict(x).reshape((-1,))

    def get_feature_importances(self, features):
        return get_feature_importance(self, features)

    def save_model(self, fname):
        import pickle
        with open(fname, 'wb') as f:
            pickle.dump(self.model, f)

