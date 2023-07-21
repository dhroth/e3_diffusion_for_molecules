from rdkit import Chem
from rdkit.Chem import AllChem
import os
import pickle
import torch
import numpy as np
from argparse import Namespace
from qm9.models import get_model
from qm9.sampling import sample
from qm9.rdkit_functions import mol2smiles, build_molecule
from qm9 import dataset
from configs.datasets_config import get_dataset_info
import ase
from ase.visualize import view
from ase import Atoms
import psi4
from psi4_chain import get_ef
import re

N_MC_STEPS = 5000

def main(epoch):
    torch.manual_seed(4)
    np.random.seed(1)

    psi4.set_memory("32 GB")

    model_path = "outputs/edm_qm9"
    args_fn = os.path.join(model_path, "args_{}.pickle").format(epoch)

    with open(args_fn, "rb") as f:
        args = pickle.load(f)

    if not hasattr(args, "normalization_factor"):
        args.normalization_factor = 1
    if not hasattr(args, "aggregation_method"):
        args.aggregation_method = "sum"

    args.cuda = not args.no_cuda and torch.cuda.is_available()
    args.device = torch.device("cuda" if args.cuda else "cpu")

    dataset_info = get_dataset_info(args.dataset, args.remove_h)
    model, _, _ = get_model(args, args.device, dataset_info, None)
    model.to(args.device)

    model_fn = "generative_model_ema_{}.npy" if args.ema_decay > 0 else "generative_model_{}.npy"
    model_fn = os.path.join(model_path, model_fn.format(epoch))
    state_dict = torch.load(model_fn, map_location=args.device)
    model.load_state_dict(state_dict)

    atom_encoder = dataset_info["atom_encoder"]
    atom_decoder = dataset_info["atom_decoder"]

    args.batch_size = 1
    dataloaders, charge_scale = dataset.retrieve_dataloaders(args)
    #first_datum = next(iter(dataloaders["valid"]))

    for data_idx, datum in enumerate(dataloaders["valid"]):
        if data_idx > 1500:
            break

        # [0] to get first datum in "batch" of size 1
        xyz = datum["positions"][0]
        xyz -= xyz.mean(dim=0)
        one_hot = datum["one_hot"][0]
        atom_types = one_hot.float().argmax(dim=1)

        mol = build_molecule(xyz, atom_types, dataset_info)
        smiles = mol2smiles(mol)
        if smiles is None:
            continue
        n_parens = sum([1 if c == "(" else 0 for c in smiles])
        has_cycles = re.search("\\d", smiles) is not None
        only_singles = "#" not in smiles and "=" not in smiles
        if has_cycles or (not only_singles) or n_parens >= 15:
            continue

        out_dir = "outputs/qm9_mc/flexible_mols/diffusion/T65/{:0>4d}".format(data_idx)
        if not os.path.exists(out_dir):
            os.mkdir(out_dir)

        symbol2num = {"H": 1, "C": 6, "N": 7, "O": 8, "F": 9}
        atomic_nums = [symbol2num[dataset_info["atom_decoder"][a]] for a in atom_types]
        atomic_nums = torch.tensor(atomic_nums)
        num_atoms = xyz.shape[0]

        max_n_nodes = dataset_info["max_n_nodes"]
        #charges = atomic_nums.view(-1, 1)
        charges = datum["charges"][0].view(-1,1)
        if (charges - atomic_nums.view(-1,1)).abs().sum() != 0:
            print("WARNING: skipping charged data_idx {}".format(data_idx))
            continue

        one_hot_padded = torch.zeros((1, max_n_nodes, one_hot.shape[1]),
                                     dtype=torch.float32)
        one_hot_padded[:,:num_atoms,:] = one_hot
        charges_padded = torch.zeros((1, max_n_nodes, charges.shape[1]),
                                     dtype=torch.float32)
        charges_padded[:,:num_atoms,:] = charges


        gs = Atoms(numbers=atomic_nums,
                   positions=xyz,
                   charges=(charges.view(-1) - atomic_nums))
        ase.io.write(os.path.join(out_dir, "gs.xyz"), gs)
        #gs_e, gs_f = get_ef(gs)

        mc_chain = []
        for mc_step in range(N_MC_STEPS):
            xyz_padded = torch.zeros((1, max_n_nodes, xyz.shape[1]),
                                     dtype=torch.float32)
            xyz_padded[:,:num_atoms,:] = xyz

            fix_noise = {"x": xyz_padded.cuda(),
                         "h_categorical": one_hot_padded.cuda(),
                         "h_integer": charges_padded.cuda()}

            diffused_one_hot, diffused_charges, diffused_x, diffused_node_mask, chain = \
                sample(args, args.device, model, dataset_info,
                       nodesxsample=torch.tensor([num_atoms]), fix_noise=fix_noise,
                       start_T=65, end_T=64)

            chain = torch.flip(chain, dims=[0])

            # returned as a batch with padding. Get first out of batch & remove padding
            #diffused_one_hot = diffused_one_hot[diffused_node_mask[:,:,0] == 1].cpu()
            #diffused_charges = diffused_charges[diffused_node_mask[:,:,0] == 1].view(-1).cpu()
            #diffused_x = diffused_x[diffused_node_mask[:,:,0] == 1].cpu()

            # make sure diffusion didn't change the atom types or charges
            numbers = [[symbol2num[dataset_info["atom_decoder"][a]]
                        for a in chain[i,:num_atoms,3:8].float().argmax(dim=1)]
                       for i in range(chain.shape[0])]
            numbers = torch.tensor(numbers).cpu()
            charges = chain[:,:num_atoms,8].round().cpu()
            if (numbers[0] - atomic_nums).abs().sum() != 0:
                print("WARNING: diffusion changed atomic numbers", data_idx, mc_step)
            #if (charges[0] - atomic_nums).abs().sum() != 0:
            #    print("WARNING: diffusion changed charges", data_idx, mc_step)
            xyz = chain[0,:num_atoms,:3]
            mc_chain.append(xyz.cpu())

            a = Atoms(numbers=atomic_nums,
                      positions=xyz.cpu(),
                      charges=(charges[0] - atomic_nums))
            ase.io.write(os.path.join(out_dir, "step_{:0>4d}.xyz".format(mc_step)), a)

        continue
        es = []
        for xyz in mc_chain:
            e, f = get_ef(Atoms(numbers=atomic_nums, positions=xyz))
            es.append(e - gs_e)
            #print(e - gs_e)
        print(es)

if __name__ == "__main__":
    main(5150)
