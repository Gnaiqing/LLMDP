import argparse
from data_utils import load_local_data, load_hub_data, hub_data_paths, TextDataset, TextPairDataset
from sampler import get_sampler
from lf_agent import get_lf_agent
from lf_family import check_all_class, apply_lfs
from label_model import get_label_model, Snorkel, MajorityLabelVoter
from end_model import train_disc_model, evaluate_disc_model
from utils import append_results, append_history, save_results
import numpy as np
from sklearn.metrics import accuracy_score
from tqdm import trange
import wandb
from pathlib import Path
import os


def main(args):
    if args.dataset_path not in hub_data_paths:
        train_dataset, valid_dataset, test_dataset, warmup_dataset = load_local_data(args.dataset_path,
                                                                                     args.dataset_name,
                                                                                     args.feature_extractor)
    else:
        train_dataset, valid_dataset, test_dataset, warmup_dataset = load_hub_data(args.dataset_path,
                                                                                   args.dataset_name,
                                                                                   args.feature_extractor)
    print(f"Dataset path: {args.dataset_path}, name: {args.dataset_name}")
    print(f"Train size: {len(train_dataset)}")
    print(f"Valid size: {len(valid_dataset)}")
    print(f"Test size: {len(test_dataset)}")
    print(f"Example:", train_dataset[0]["sentence"])
    print(f"Label:", train_dataset[0]["label"])
    if args.save_wandb:
        group_id = wandb.util.generate_id()
        config_dict = vars(args)
        config_dict["method"] = "LLMDP"
        config_dict["group_id"] = group_id

    rng = np.random.default_rng(args.seed)
    for run in range(args.runs):
        if args.save_wandb:
            wandb.init(
                project="LLMDP",
                config=config_dict
            )
            wandb.define_metric("test_acc", summary="mean")
            wandb.define_metric("test_auc", summary="mean")
            wandb.define_metric("test_f1", summary="mean")
            wandb.define_metric("train_precision", summary="mean")
            wandb.define_metric("train_coverage", summary="mean")

        seed = rng.choice(10000)
        sampler = get_sampler(train_dataset=train_dataset,
                              sampler_type=args.sampler
                              )
        lf_agent = get_lf_agent(train_dataset=train_dataset,
                                valid_dataset=valid_dataset,
                                agent_type=args.lf_agent,
                                lf_type=args.lf_type,
                                filter_methods=args.lf_filter,
                                acc_threshold=args.lf_acc_threshold,
                                model=args.lf_llm_model,
                                seed=seed,
                                )
        al_model = None
        lfs = []
        lf_accs = []
        lf_covs = []
        results = {
            "num_query": [],
            "lf_num" : [],
            "lf_acc_avg": [],
            "lf_cov_avg": [],
            "train_precision": [],
            "train_coverage": [],
            "test_acc": [],
            "test_f1": [],
            "test_auc": []
        }
        history = {}
        label_model = None
        for t in range(args.num_query):
            query_idx = sampler.sample(al_model=al_model)[0]
            if args.display:
                print("Query: ", train_dataset[query_idx]["sentence"])
            lf = lf_agent.create_lf(query_idx)
            if lf is not None:
                if args.display:
                    print("LF: ", lf.info())
                lfs.append(lf)
                lf_cov, lf_acc = lf.get_cov_acc(train_dataset)
                lf_covs.append(lf_cov)
                lf_accs.append(lf_acc)
                lf_labels = [lf.label for lf in lfs]
                if check_all_class(lf_labels, train_dataset.n_class):
                    if len(lfs) > 3:
                        label_model = get_label_model(args.label_model, train_dataset.n_class)
                    else:
                        label_model = get_label_model("mv", train_dataset.n_class)

                    L_train = train_dataset.apply_lfs(lfs)
                    L_val = valid_dataset.apply_lfs(lfs)
                    if isinstance(label_model, Snorkel):
                        label_model.fit(L_train, L_val, valid_dataset.ys)

            else:
                if args.display:
                    print("LF: None")

            history = append_history(history, train_dataset[query_idx], lf)
            if t % args.train_iter == args.train_iter - 1:
                if label_model is not None:
                    # train discriminative model
                    ys_tr = label_model.predict(L_train)
                    ys_tr_soft = label_model.predict_proba(L_train)
                    covered_indices = (np.max(L_train, axis=1) != -1) & (ys_tr != -1) # indices covered by LFs
                    xs_tr = train_dataset.xs_feature[covered_indices, :]
                    ys_tr = ys_tr[covered_indices]
                    ys_tr_soft = ys_tr_soft[covered_indices, :]
                    # evaluate label quality
                    train_coverage = np.mean(covered_indices)
                    train_precision = accuracy_score(train_dataset.ys[covered_indices], ys_tr)
                    lf_acc_avg = np.mean(lf_accs)
                    lf_cov_avg = np.mean(lf_covs)
                    if check_all_class(ys_tr, train_dataset.n_class):
                        disc_model = train_disc_model(model_type=args.end_model,
                                                      xs_tr=xs_tr,
                                                      ys_tr_soft=ys_tr_soft,
                                                      ys_tr_hard=ys_tr,
                                                      valid_dataset=valid_dataset,
                                                      warmup_dataset=warmup_dataset,
                                                      soft_training=args.use_soft_labels,
                                                      seed=seed)
                        # evaluate end model performance
                        test_perf = evaluate_disc_model(disc_model, test_dataset)
                    else:
                        test_perf = {"acc": np.nan, "f1": np.nan, "auc": np.nan}

                    cur_result = {
                        "num_query": t+1,
                        "lf_num": len(lfs),
                        "lf_acc_avg": lf_acc_avg,
                        "lf_cov_avg": lf_cov_avg,
                        "train_precision": train_precision,
                        "train_coverage": train_coverage,
                        "test_acc": test_perf["acc"],
                        "test_f1": test_perf["f1"],
                        "test_auc": test_perf["auc"]
                    }
                    results = append_results(results, cur_result)
                    if args.save_wandb:
                        wandb.log(cur_result)

                    if args.display:
                        print(f"After {t+1} iterations:")
                        print(f"    Avg LF accuracy: {lf_acc_avg:.3f}")
                        print(f"    Avg LF coverage: {lf_cov_avg:.3f}")
                        print(f"    Train precision: {train_precision:.3f}")
                        print(f"    Train coverage: {train_coverage:.3f}")
                        print(f"    Test Accuracy:", test_perf["acc"])
                        print(f"    Test AUC:", test_perf["auc"])
                        print(f"    Test F1:", test_perf["f1"])
                else:
                    lf_acc_avg = np.mean(lf_accs)
                    lf_cov_avg = np.mean(lf_covs)
                    cur_result = {
                        "num_query": t + 1,
                        "lf_num": len(lfs),
                        "lf_acc_avg": lf_acc_avg,
                        "lf_cov_avg": lf_cov_avg,
                        "train_precision": np.nan,
                        "train_coverage": np.nan,
                        "test_acc": np.nan,
                        "test_f1": np.nan,
                        "test_auc": np.nan
                    }
                    results = append_results(results, cur_result)
                    if args.save_wandb:
                        wandb.log(cur_result)
                    if args.display:
                        print(f"After {t+1} iterations:")
                        print(f"    No enough LFs. Skip training end model.")

        test_acc_avg = np.mean(results["test_acc"])
        test_auc_avg = np.mean(results["test_auc"])
        test_f1_avg = np.mean(results["test_f1"])
        if args.display:
            print(f"Summary stats of run {run}")
            print("     Avg Test Accuracy: ", test_acc_avg)
            print("     Avg Test AUC: ", test_auc_avg)
            print("     Avg Test F1: ", test_f1_avg)

        results_path = Path(args.results_dir) / args.dataset_name
        if not os.path.exists(results_path):
            os.makedirs(results_path)

        save_results(results, results_path / f"results_{args.tag}_{run}.csv")
        save_results(results, results_path / f"history_{args.tag}_{run}.csv")
        if args.save_wandb:
            wandb.finish()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    # dataset
    parser.add_argument("--dataset-path", type=str, default="glue", help="dataset path (or benchmark name)")
    parser.add_argument("--dataset-name", type=str, default="sst2", help="dataset name")
    parser.add_argument("--feature-extractor", type=str, default="tfidf", help="feature for training end model")
    # sampler
    parser.add_argument("--sampler", type=str, default="passive", help="sample selector")
    # data programming
    parser.add_argument("--label-model", type=str, default="snorkel", help="label model used in DP paradigm")
    parser.add_argument("--use-soft-labels", action="store_true", help="set to true if use soft labels when training end model")
    parser.add_argument("--end-model", type=str, default="logistic", help="end model in DP paradigm")
    # label function
    parser.add_argument("--lf-agent", type=str, default="simulated", help="agent that return candidate LFs")
    parser.add_argument("--lf-type", type=str, default="keyword", help="LF family")
    parser.add_argument("--lf-filter", type=str, nargs="+", default=["acc", "unique"], help="filters for simulated agent")
    parser.add_argument("--lf-acc-threshold", type=float, default=0.5, help="LF accuracy threshold for simulated agent")
    parser.add_argument("--lf-llm-model", type=str, default="gpt-3.5-turbo")
    # experiment
    parser.add_argument("--num-query", type=int, default=50, help="total selected samples")
    parser.add_argument("--train-iter", type=int, default=5, help="evaluation interval")
    parser.add_argument("--results-dir", type=str,default="./results")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--display", action="store_true")
    parser.add_argument("--save-wandb", action="store_true")
    parser.add_argument("--tag", type=str, default="0")
    args = parser.parse_args()
    main(args)

