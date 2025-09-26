import math
import itertools

from ..datas import boxes
from ..utils import printModel, dicModel
class ShippingController:
    def __init__(self):
        self.BOX_KG = 1.0

    def list_all_npr(self, data, r):
        """
          Generates and lists all permutations of length 'r' from the given 'iterable'.

          Args:
            iterable: The input iterable (e.g., list, tuple, string) from which to draw elements.
            r: The length of each permutation.

          Returns:
            A list containing all permutations as tuples.
          """
        return list(itertools.permutations(data, r))

    def findPacking(self, l, w, h, kg, min_pcs, max_pcs=None):
        """
        :param l: single product L (cm)
        :param w: single product W (cm)
        :param h: single product H (cm)
        :param kg: single product weight (kg)
        :param min_pcs: minimum pcs per carton
        """
        packing_results = []

        for box in boxes.BoxDimsions:
            # list out all the combination
            for box_dim in self.list_all_npr(list(box.values()), 3):
                L_c = math.floor(box_dim[0] / l)
                W_c = math.floor(box_dim[1] / w)
                H_c = math.floor(box_dim[2] / h)

                # calculate the package information
                total_c = L_c * W_c * H_c
                total_kg = total_c * kg + self.BOX_KG

                # calculate the used volume and efficient
                used_volume = l * w * h * total_c
                box_volume = box_dim[0] * box_dim[1] * box_dim[2]
                box_efficient = used_volume / box_volume

                if box_volume >= used_volume and used_volume > 0 and total_c >= min_pcs and (not max_pcs or total_c <= max_pcs):
                    data = {
                        "box_dims": f"{box_dim[0]} x {box_dim[1]} x {box_dim[2]}",
                        f"box_dim[0] / l": f"{box_dim[0]:.0f} / {l:.0f}",
                        f"box_dim[1] / w": f"{box_dim[1]:.0f} / {w:.0f}",
                        f"box_dim[2] / h": f"{box_dim[2]:.0f} / {h:.0f}",
                        "L_c": L_c,
                        "W_c": W_c,
                        "H_c": H_c,
                        "total_c": total_c,
                        "total_kg": total_kg,
                        "used_volume": used_volume,
                        "box_volume": box_volume,
                        "box_efficient": box_efficient * 100
                    }
                    # append the result
                    packing_results.append(data)

        sorted_packing_results = dicModel.sort_dict(packing_results, [('total_c', True), ('box_efficient', True)], weights=[0.7, 0.3])
        # printing pack results
        for result in sorted_packing_results:
            printModel.print_dict(result)
