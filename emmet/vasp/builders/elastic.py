import numpy as np
import logging
from datetime import datetime
from itertools import chain

from monty.json import jsanitize

from pymatgen import Structure
from pymatgen.analysis.elasticity.elastic import ElasticTensor
from pymatgen.analysis.elasticity.strain import Strain, Deformation
from pymatgen.analysis.elasticity.stress import Stress
from pymatgen.analysis.structure_matcher import StructureMatcher, ElementComparator
from pymatgen.symmetry.analyzer import SpacegroupAnalyzer

from fireworks.utilities.fw_serializers import recursive_serialize

from maggma.builder import Builder

from atomate.utils.utils import get_mongolike

import tqdm


__author__ = "Joseph Montoya, Shyam Dwaraknath"
__maintainer__ = "Joseph Montoya"
__email__ = "montoyjh@lbl.gov"


logger = logging.getLogger(__name__)

class ElasticBuilder(Builder):
    def __init__(self, tasks, elasticity, materials,
                 query={}, incremental=True, **kwargs):
        """
        Creates a elastic collection for materials

        Args:
            tasks (Store): Store of task documents
            elastic (Store): Store of elastic properties
            materials (Store): Store of materials properties
            query (dict): dictionary to limit tasks to be analyzed
            incremental (bool): whether or not to use a lu_filter based
                on the current datetime
        """

        self.tasks = tasks
        self.elasticity = elasticity
        self.materials = materials
        self.query = query
        self.incremental = incremental
        self.start_date = datetime.utcnow()

        super().__init__(sources=[tasks],
                         targets=[elasticity],
                         **kwargs)

    def connect(self):
        self.tasks.connect()
        self.elasticity.connect()
        self.materials.connect()

    def get_items(self):
        """
        Gets all items to process into materials documents

        Returns:
            generator or list relevant tasks and materials to process into materials documents
        """

        self.logger.info("Elastic Builder Started")
        self.logger.debug("Adding indices")
        self.tasks.ensure_index("nsites")
        self.tasks.ensure_index("formula_pretty")

        # Get only successful elastic deformation tasks with parent structure
        q = dict(self.query)
        q["state"] = "successful"
        # q["task_label"] = "elastic deformation"

        # only consider tasks that have been updated since materials was last updated
        if self.incremental:
            self.logger.info("Ensuring indices on lu_field for sources and targets")
            self.tasks.ensure_index(self.tasks.lu_field)
            self.elasticity.ensure_index(self.elasticity.lu_field)
            q.update(self.tasks.lu_filter(self.elasticity))

        # TODO: Ensure appropriately selective DFT params - input.incar.GGA, input.incar.ENCUT
        #       for kpoints, designate some cutoff for number
        # TODO: mpworks discrepancy in original input, probably going to just have to
        #       let it lie as a distinguisher between atomate/mpworks
        mutually_exclusive_params = ["formula_pretty"]
        return_props = ['calcs_reversed', 'output', 'input', 'completed_at',
                        'transmuter', 'task_id', 'task_label']
        self.logger.debug("Getting criteria")
        criterias = self.tasks.distinct(mutually_exclusive_params, criteria=q)
        self.logger.debug("Found {} unique formulas".\
                          format(len(criterias)))
        material_dict = generate_formula_dict(self.materials)
        # hackish sieve to ensure parity between material dict and tasks
        criterias = [c for c in criterias if c['formula_pretty'] in material_dict]
        for n, crit in enumerate(criterias):
            crit.update(q)
            tasks = list(self.tasks.query(criteria=crit, properties=return_props))

            # Group by material_id
            # TODO: refactor for task sets without structure opt
            logger.debug("Processing formula {}, {} of {}".format(
                crit['formula_pretty'], n, len(criterias)))
            # TODO: refactor for parallelization
            formula_mat_dict = material_dict[crit['formula_pretty']]
            yield tasks, formula_mat_dict.copy()
            # else:
            #    yield [], {}
            #    logging.warning("No material with formula {}".format(
            #        crit['formula_pretty']))
            # grouped = group_by_material_id(self.materials, tasks)
            # yield grouped
            """
            for material_id, task_sets in grouped.items():
                self.logger.debug("Processing {} : {} of {}".format(
                    crit['formula_pretty'], n, len(criterias)))
                yield material_id, task_sets
            """

    def process_item(self, item):
        """
        Process the tasks and materials into a elasticity collection

        Args:
            item: a dictionary of documents keyed by materials id

        Returns:
            an elasticity document
        """
        all_docs = []
        tasks, material_dict = item
        if not tasks:
            return all_docs
        grouped = group_by_material_id(material_dict, tasks)
        for mp_id, task_sets in grouped.items():
            elastic_docs = []
            for opt_task, defo_tasks in task_sets:
                elastic_doc = get_elastic_analysis(opt_task, defo_tasks)
                if elastic_doc:
                    elastic_docs.append(elastic_doc)

            if not elastic_docs:
                logger.warning("No elastic doc for mp_id {}".format(mp_id))
                continue
            # For now just do the most recent one that's not failed
            sorted(elastic_docs, key=lambda x: (x['state'], x['completed_at']))
            final_doc = elastic_docs[-1]
            c_ijkl = ElasticTensor.from_voigt(final_doc['elastic_tensor'])
            structure = final_doc['optimized_structure']
            formula = structure.composition.reduced_formula
            elements = [s.symbol for s in structure.composition.elements]
            chemsys = '-'.join(elements)
            final_doc.update(c_ijkl.property_dict)
            final_doc.update(c_ijkl.get_structure_property_dict(structure))

            elastic_summary = {'material_id': mp_id,
                               'all_elastic_fits': elastic_docs,
                               'elasticity': final_doc,
                               'pretty_formula': formula,
                               'chemsys': chemsys,
                               'elements': elements,
                               'last_updated': self.elasticity.lu_field}
            all_docs.append(elastic_summary)
            # elastic_summary.update(final_doc)

        return all_docs


    def update_targets(self, items):
        """
        Inserts the new elasticity documents into the elasticity collection

        Args:
            items ([dict]): list of elasticity docs
        """
        import nose; nose.tools.set_trace()

        items = chain.from_iterable(items)
        items = [jsanitize(doc, strict=True) for doc in items]
        self.logger.info("Updating {} elastic documents".format(len(items)))

        # self.elasticity.collection.insert_many(items)
        self.elasticity.update(items, key='material_id')

    def finalize(self, items):
        """
        if self.materials:
            # Get all docs where there's no mp_id
            logger.info("Assigning mp ids:")
            docs_without_id = self.elasticity.query(
                ["parent_structure"], criteria={"material_id": None})
            for doc in docs_without_id:
            """

        pass

    def _find_mp_id(self, structure, structure_matcher=None):
        sm = structure_matcher or StructureMatcher()
        sga = SpacegroupAnalyzer(structure)
        candidates = self.materials.query(
                ['structure', 'material_id'],
                {"formula_pretty": structure.composition.formula_reduced,
                 "spacegroup.number": sga.space_group.number})
        for candidate in candidates:
            c_structure = Structure.from_dict(candidate['structure'])
            if sm.fit(c_structure, structure):
                return candidate['material_id']

def get_elastic_analysis(opt_task, defo_tasks):
    """
    Performs the analysis of opt_tasks and defo_tasks necessary for
    an elastic analysis
    
    Args:
        opt_task: task doc corresponding to optimization
        defo_tasks: task_doc corresponding to deformations

    Returns:
        elastic document with fitted elastic tensor and analysis

    """
    elastic_doc = {"warnings": []}
    opt_struct = Structure.from_dict(opt_task['output']['structure'])
    d_structs = [Structure.from_dict(d['output']['structure'])
                 for d in defo_tasks]
    defos = [calculate_deformation(opt_struct, def_structure)
             for def_structure in d_structs]

    # Warning if deformation is not equivalent to stored deformation
    stored_defos = [d['transmuter']['transformation_params'][0]\
                     ['deformation'] for d in defo_tasks]
    # defos, stored_defos = np.array(defos), np.array(stored_defos)
    if not np.allclose(defos, stored_defos, atol=1e-5):
        wmsg = "Inequivalent stored and calc. deformations."
        logger.warning(wmsg)
        elastic_doc["warnings"].append(wmsg)

    # Collect all fitting data and task ids
    defos = [Deformation(d) for d in defos]
    strains = [d.green_lagrange_strain for d in defos]
    vasp_stresses = [d['calcs_reversed'][0]['output']['ionic_steps'][-1]\
                     ['stress'] for d in defo_tasks]
    cauchy_stresses = [-0.1 * Stress(s) for s in vasp_stresses]
    pk_stresses = [Stress(s.piola_kirchoff_2(d))
                   for s, d in zip(cauchy_stresses, defos)]
    defo_task_ids = [d['task_id'] for d in defo_tasks]

    # Determine whether data is sufficient to fit tensor
    # If raw data is insufficient but can be symmetrically transformed
    # to provide a sufficient set, use the expanded set with appropriate
    # symmetry transformations, fstresses/strains are "fitting
    # strains" below.
    vstrains = [s.voigt for s in strains]
    if np.linalg.matrix_rank(vstrains) < 6:
        symmops = SpacegroupAnalyzer(opt_struct).get_symmetry_operations()
        fstrains = [[s.transform(symmop) for symmop in symmops] for s in strains]
        fstrains = list(chain.from_iterable(fstrains))
        vfstrains = [s.voigt for s in fstrains]
        if not np.linalg.matrix_rank(vfstrains) == 6:
            logger.warning("Insufficient data to form SOEC")
            elastic_doc['warnings'].append("insufficient strains")
            return None
        else:
            fstresses = [[s.transform(symmop) for symmop in symmops] for s in pk_stresses]
            fstresses = list(chain.from_iterable(fstresses))
    else:
        fstrains = strains
        fstresses = pk_stresses

    et_raw = ElasticTensor.from_pseudoinverse(fstrains, fstresses)
    et = et_raw.voigt_symmetrized.convert_to_ieee(opt_struct)
    defo_tasks = sorted(defo_tasks, key=lambda x: x['completed_at'])
    input = opt_task['input']
    input.pop('structure')
    input['kpoints'] = opt_task['calcs_reversed'][0]['input']['kpoints']

    elastic_doc.update({"deformation_task_ids": defo_task_ids,
                        "optimization_task_id": opt_task['task_id'],
                        "pk_stresses": pk_stresses,
                        "cauchy_stresses": cauchy_stresses,
                        "strains": strains,
                        "deformations": defos,
                        "elastic_tensor": et.voigt,
                        "elastic_tensor_raw": et_raw.voigt,
                        "optimized_structure": opt_struct,
                        "completed_at": defo_tasks[-1]['completed_at'],
                        "optimization_input": input})

    # Process input
    elastic_doc['warnings'] = get_warnings(et, opt_struct) or None
    # TODO: process MPWorks metadata?
    # TODO: higher order?
    # TODO: fitting method?
    # TODO: add some of the relevant DFT params
    elastic_doc['state'] = "filter_failed" if elastic_doc['warnings']\
        else "successful"
    return elastic_doc


# TODO: clean up unnecessary task/doc dichotomy
def group_by_material_id(materials_dict, docs, tol=1e-6,
                         structure_matcher=None):
    """
    Groups a collection of documents by material id
    as found in a materials collection
    
    Args:
        materials_dict (dict): dictionary of structures keyed by material_id
        docs ([dict]): list of documents 
        tol: tolerance for lattice grouping
        structure_matcher (StructureMatcher): structure
            matcher for finding equivalent structures

    Returns:
        documents grouped by material_id from the materials
        collection
    """
    sm = structure_matcher or StructureMatcher()
    tasks_by_opt = group_deformations_by_optimization_task(docs, tol)
    task_sets_by_mp_id = {}
    for opt_task, defo_tasks in tasks_by_opt:
        structure = Structure.from_dict(opt_task['output']['structure'])
        match = False
        for c_id, candidate in materials_dict.items():
            c_structure = Structure.from_dict(candidate)
            if sm.fit(c_structure, structure):
                mp_id = c_id
                match = True
                break
        if match:
            if mp_id in task_sets_by_mp_id:
                task_sets_by_mp_id[mp_id].append((opt_task, defo_tasks))
            else:
                task_sets_by_mp_id[mp_id] = [(opt_task, defo_tasks)]
    return task_sets_by_mp_id


def group_deformations_by_optimization_task(docs, tol=1e-6):
    """
    Groups a set of deformation tasks by equivalent lattices
    to an optimization task.  Basically the same as
    group_by_parent_lattice, except does an additional
    step of finding the optimization and using that
    as the grouping parameter.  Also filters document
    sets that don't include an optimization and deformations.
    """
    # TODO: this could prolly be refactored to be more generally useful
    tasks_by_lattice = group_by_parent_lattice(docs, tol)
    tasks_by_opt_task = []
    for lattice, task_set in tasks_by_lattice:
        opt_struct_tasks = [task for task in task_set
                           if task['task_label']=='structure optimization']
        deformation_tasks = [task for task in task_set
                             if task['task_label']=='elastic deformation']
        opt_struct_tasks.reverse()
        if opt_struct_tasks and deformation_tasks:
            tasks_by_opt_task.append((opt_struct_tasks[-1], deformation_tasks))
        else:
            logger.warning("No structure opt matching tasks")
    return tasks_by_opt_task


def group_by_parent_lattice(docs, tol=1e-6):
    """
    Groups a set of documents by parent lattice equivalence

    Args:
        docs ([{}]): list of documents e. g. dictionaries or cursor
        tol (float): tolerance for equivalent lattice finding using,
            np.allclose, default 1e-10
    """
    docs_by_lattice = []
    for doc in docs:
        sim_lattice = get_mongolike(doc, "output.structure.lattice.matrix")

        if "deformation" in doc['task_label']:
            # Note that this assumes only one transformation, deformstructuretransformation
            defo = doc['transmuter']['transformation_params'][0]['deformation']
            parent_lattice = np.dot(sim_lattice, np.transpose(np.linalg.inv(defo)))
        else:
            parent_lattice = np.array(sim_lattice)
        match = False
        for unique_lattice, lattice_docs in docs_by_lattice:
            match = np.allclose(unique_lattice, parent_lattice, atol=tol)
            if match:
                lattice_docs.append(doc)
                break
        if not match:
            docs_by_lattice.append([parent_lattice, [doc]])
    return docs_by_lattice


def calculate_deformation(undeformed_structure, deformed_structure):
    """
    
    Args:
        undeformed_structure (Structure): undeformed structure
        deformed_structure (Structure): deformed structure

    Returns:
        deformation matrix
    """
    ulatt = undeformed_structure.lattice.matrix
    dlatt = deformed_structure.lattice.matrix
    return np.transpose(np.dot(np.linalg.inv(ulatt), dlatt))


def get_warnings(elastic_tensor, structure):
    """
    Generates all warnings that apply to a fitted elastic tensor
    
    Args:
        elastic_tensor (ElasticTensor): elastic tensor for which
            to determine warnings
        structure (Structure): structure for which elastic tensor
            is determined

    Returns:
        list of warnings

    """
    warnings = []
    if any([s.is_rare_earth_metal for s in structure.species]):
        warnings.append("Contains a rare earth element")
    eigs, eigvecs = np.linalg.eig(elastic_tensor.voigt)
    if np.any(eigs < 0.0):
        warnings.append("Elastic tensor has a negative eigenvalue")
    c11, c12, c13 = elastic_tensor.voigt[0, 0:3]
    c23 = elastic_tensor.voigt[1, 2]

    # TODO: these should be revisited at some point, are they complete?
    #       I think they might only apply to cubic systems
    if not (abs((c11 - c12) / c11) < 0.05 or c11 < c12):
        warnings.append("c11 and c12 are within 5% or c12 is greater than c11")
    if not (abs((c11 - c13) / c11) < 0.05 or c11 < c13):
        warnings.append("c11 and c13 are within 5% or c13 is greater than c11")
    if not (abs((c11 - c23) / c11) < 0.05 or c11 < c23):
        warnings.append("c11 and c23 are within 5% or c23 is greater than c11")

    moduli = ["k_voigt", "k_reuss", "k_vrh", "g_voigt", "g_reuss", "g_vrh"]
    moduli_array = np.array([getattr(elastic_tensor, m) for m in moduli])
    if np.any(moduli_array) < 2:
        warnings.append("One or more K, G below 2 GPa")

    return warnings

def generate_formula_dict(materials_store, query=None):
    """
    
    Args:
        materials_store (Store): store of materials 

    Returns:
        Nested dictionary keyed by formula-mp_id with structure values.

    """
    pipeline = [{'$match': query}] if query else []
    pipeline.extend([{'$project': {'structure':1, 'pretty_formula': 1,
                                   'material_id': 1}},
                     {'$group': {'_id': '$pretty_formula',
                                 'mp_ids': {'$push': '$material_id'},
                                 'structures': {'$push': '$structure'}}}])
    results = list(materials_store.collection.aggregate(pipeline))
    formula_dict = {}
    for result in tqdm.tqdm(results):
        formula_dict[result['_id']] = dict(zip(result['mp_ids'],
                                               result['structures']))
    return formula_dict