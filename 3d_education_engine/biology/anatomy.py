"""Anatomy: which source structures make up each named part of a model.

A RECIPE says how to build a multi-part model from BodyParts3D: each part is a
set of FMA (Foundational Model of Anatomy) concepts, whose element meshes are
merged. Parts are claimed in order and an element belongs to the FIRST part
that claims it -- BodyParts3D's concepts overlap (the mitral valve's element
list includes two aortic-valve cusps, because of the aortic-mitral fibrous
continuity), so the order is part of the anatomy, not an accident.

Nothing here is drawn by hand: every triangle comes from the source database.
Where the curriculum names a structure the source does not model separately
(the interventricular septum), the part is listed as unavailable with a note,
rather than invented.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# Textbook convention, stated on screen: blue = the side carrying deoxygenated
# blood, red = oxygenated. Real heart tissue is not coloured like this.
DEOXY_CHAMBER, DEOXY_VESSEL = "#5b7fd6", "#3f5fc0"
OXY_CHAMBER, OXY_VESSEL = "#d65f5a", "#dc3f3a"
VALVE, CORONARY_ARTERY, CARDIAC_VEIN = "#f0e2a8", "#e8963d", "#8a5ab8"


@dataclass
class PartRecipe:
    id: str
    name: str
    concepts: list[str]                  # FMA ids whose elements make up the part
    color: str
    description: str
    aliases: list[str] = field(default_factory=list)
    minus: list[str] = field(default_factory=list)    # FMA ids whose elements are excluded
    remainder_of: str | None = None      # take what is left of this concept after earlier parts
    keep_z_above: float | None = None    # trim: keep geometry within this many mm below the heart
    note: str = ""


@dataclass
class ModelRecipe:
    model_id: str
    name: str
    root_concept: str
    parts: list[PartRecipe]
    unavailable: dict[str, dict]
    groups: dict[str, list[str]]
    group_aliases: dict[str, list[str]]
    aliases: list[str]
    topics: list[str]
    grades: list[str]
    relationships: dict
    notes: list[str]
    animations: list[str] = field(default_factory=list)   # names models/loader.py knows
    scale_level: str = "organ"
    low_lod_cells: int | None = None     # None: the converter's LOW_LOD_TARGET_CELLS
    # The parts named when the model first appears, when there are too many to
    # name them all (the engine's cap is 16). None: every part.
    default_labels: list[str] | None = None
    # Which way the model faces the camera when it opens: "-y" is the front
    # of the body; a brain is drawn from the side, face to the left.
    front: str = "-y"


HEART = ModelRecipe(
    model_id="biology.anatomy.heart",
    name="Human Heart",
    root_concept="FMA7088",
    parts=[
        # Valves first: see the module docstring for why order matters.
        PartRecipe("aortic_valve", "Aortic valve", ["FMA7252", "FMA7253", "FMA7254"], VALVE,
                   "Semilunar valve with three cusps between the left ventricle and the aorta. "
                   "It opens when the left ventricle contracts and closes to stop blood flowing back.",
                   ["aortic valves"]),
        PartRecipe("pulmonary_valve", "Pulmonary valve", ["FMA7246"], VALVE,
                   "Semilunar valve with three cusps between the right ventricle and the pulmonary trunk.",
                   ["pulmonic valve", "pulmonary valves"]),
        PartRecipe("tricuspid_valve", "Tricuspid valve", ["FMA7234"], VALVE,
                   "Valve with three leaflets between the right atrium and right ventricle. It closes "
                   "when the ventricle contracts, so blood cannot flow back into the atrium.",
                   ["right atrioventricular valve"]),
        PartRecipe("mitral_valve", "Mitral valve", ["FMA7235"], VALVE,
                   "Valve with two leaflets (the bicuspid valve) between the left atrium and left "
                   "ventricle. Shown with its fibrous ring.",
                   ["bicuspid valve", "left atrioventricular valve", "mitral valves"]),
        PartRecipe("right_atrium", "Right atrium", ["FMA7096"], DEOXY_CHAMBER,
                   "Receives deoxygenated blood from the body through the superior and inferior "
                   "venae cavae, and passes it to the right ventricle.",
                   ["right auricle", "RA", "right atrial"]),
        PartRecipe("left_atrium", "Left atrium", ["FMA7097"], OXY_CHAMBER,
                   "Receives oxygenated blood from the lungs through the pulmonary veins, and "
                   "passes it to the left ventricle.",
                   ["LA", "left auricle", "left atrial"]),
        PartRecipe("right_ventricle", "Right ventricle", ["FMA7098"], DEOXY_CHAMBER,
                   "Pumps deoxygenated blood through the pulmonary valve into the pulmonary trunk, "
                   "on its way to the lungs.",
                   ["RV", "right ventricular", "right ventricular wall"]),
        PartRecipe("left_ventricle", "Left ventricle", ["FMA7101"], OXY_CHAMBER,
                   "Pumps oxygenated blood through the aortic valve into the aorta and out to the "
                   "whole body. It has the thickest muscular wall of the four chambers.",
                   ["LV", "heart muscle of the left ventricle", "left ventricular", "left ventricular wall", "wall of the left ventricle"]),
        PartRecipe("aorta", "Aorta", ["FMA3736", "FMA3768"], OXY_VESSEL,
                   "The largest artery, carrying oxygenated blood from the left ventricle to the "
                   "body. Shown: the ascending aorta and the aortic arch.",
                   ["ascending aorta", "aortic arch", "arch of aorta"]),
        PartRecipe("pulmonary_artery", "Pulmonary trunk", ["FMA8612"], DEOXY_VESSEL,
                   "Carries deoxygenated blood from the right ventricle towards the lungs, where it "
                   "divides into the right and left pulmonary arteries (not shown).",
                   ["pulmonary artery", "pulmonary arteries", "pulmonary trunk"]),
        PartRecipe("superior_vena_cava", "Superior vena cava", ["FMA4720"], DEOXY_VESSEL,
                   "Returns deoxygenated blood from the head, neck and arms to the right atrium.",
                   ["SVC", "superior venacava"]),
        PartRecipe("inferior_vena_cava", "Inferior vena cava", ["FMA10951"], DEOXY_VESSEL,
                   "Returns deoxygenated blood from the lower body to the right atrium. Shown: "
                   "its last few centimetres before the heart.",
                   ["IVC", "inferior venacava"], keep_z_above=25.0,
                   note="Trimmed to the 25 mm below the heart; the source continues into the abdomen."),
        PartRecipe("pulmonary_veins", "Pulmonary veins",
                   ["FMA49914", "FMA49916", "FMA49911", "FMA49913"], OXY_VESSEL,
                   "Carry oxygenated blood from the lungs into the left atrium; usually four, two "
                   "from each lung. Shown: the parts outside the lungs.",
                   ["pulmonary vein"],
                   minus=["FMA68002", "FMA68004", "FMA68003", "FMA68005"]),
        PartRecipe("coronary_arteries", "Coronary arteries", ["FMA50039", "FMA50040"], CORONARY_ARTERY,
                   "The right and left coronary arteries and their branches, which supply the heart "
                   "muscle itself with oxygenated blood.",
                   ["coronary artery", "right coronary artery", "left coronary artery"]),
        PartRecipe("cardiac_veins", "Cardiac veins", [], CARDIAC_VEIN,
                   "Veins that drain the heart muscle, mostly into the coronary sinus, which empties "
                   "into the right atrium.",
                   ["coronary sinus", "cardiac vein", "coronary veins"], remainder_of="FMA7088"),
    ],
    unavailable={
        "interventricular_septum": {
            "name": "Interventricular septum",
            "aliases": ["septum", "ventricular septum"],
            "note": "BodyParts3D does not model the septum as a separate structure; in this model "
                    "it is part of the ventricle walls. It can be explained but not highlighted.",
        },
    },
    groups={
        "chambers": ["right_atrium", "right_ventricle", "left_atrium", "left_ventricle"],
        "atria": ["right_atrium", "left_atrium"],
        "ventricles": ["right_ventricle", "left_ventricle"],
        "valves": ["tricuspid_valve", "mitral_valve", "pulmonary_valve", "aortic_valve"],
        "great_vessels": ["aorta", "pulmonary_artery", "superior_vena_cava", "inferior_vena_cava",
                          "pulmonary_veins"],
        "arteries": ["aorta", "pulmonary_artery", "coronary_arteries"],
        "veins": ["superior_vena_cava", "inferior_vena_cava", "pulmonary_veins", "cardiac_veins"],
        "right_side": ["right_atrium", "right_ventricle", "tricuspid_valve", "pulmonary_valve"],
        "left_side": ["left_atrium", "left_ventricle", "mitral_valve", "aortic_valve"],
        "coronary_circulation": ["coronary_arteries", "cardiac_veins"],
    },
    group_aliases={
        "chambers": ["four chambers", "the four chambers", "heart chambers", "cavities"],
        "great_vessels": ["blood vessels", "vessels", "big vessels", "major vessels"],
        "valves": ["heart valves", "four valves"],
        "coronary_circulation": ["coronary vessels", "coronaries", "blood supply of the heart"],
        "right_side": ["right heart", "right side of the heart"],
        "left_side": ["left heart", "left side of the heart"],
    },
    aliases=["heart", "human heart", "my heart", "cardiac organ", "the heart", "hriday", "dil", "दिल", "हृदय"],
    topics=["human heart", "circulatory system", "cardiovascular system", "double circulation"],
    grades=["7", "8", "9", "10", "11", "12"],
    relationships={
        "parent": "biology.anatomy.cardiovascular_system",
        "inside": ["biology.tissue.cardiac_muscle"],
        "scale_down_to": "biology.tissue.cardiac_muscle",
        "scale_down_focus": "left_ventricle",
        # "Zoom into the left ventricle": the wall of any chamber is this tissue.
        "part_models": {c: "biology.tissue.cardiac_muscle" for c in
                        ("left_ventricle", "right_ventricle", "left_atrium", "right_atrium")},
    },
    notes=["Colours follow the textbook convention: blue = deoxygenated blood, red = oxygenated. "
           "Real heart tissue is not coloured this way."],
    animations=["heartbeat"],
)

# Bone, and the backbone's three regions tinted apart the way a textbook
# figure does; cartilage bluish-white. Real bone is all one colour.
BONE, SKULL_BONE = "#e6dcc3", "#eee6d3"
CERVICAL, THORACIC, LUMBAR, SACRAL = "#e3c79c", "#d9b98a", "#cfab7a", "#c49c6c"
CARTILAGE = "#9cc2dc"
_R = "(right and left)"

# The textbook's 206 bones less seven BodyParts3D does not model: 199 here.
SKELETON = ModelRecipe(
    model_id="biology.anatomy.skeleton",
    name="Human Skeleton",
    root_concept="FMA23876",
    parts=[
        PartRecipe("cranium", "Skull (cranium and face)",
                   ["FMA9710", "FMA49187", "FMA52734", "FMA52735", "FMA52736", "FMA52738",
                    "FMA52739", "FMA52740", "FMA52788", "FMA52789", "FMA52892", "FMA52893",
                    "FMA53645", "FMA53646", "FMA53647", "FMA53648", "FMA53649", "FMA53650",
                    "FMA53655", "FMA53656", "FMA54737", "FMA54738"], SKULL_BONE,
                   "Eight cranial bones fused into a box that protects the brain, and thirteen "
                   "face bones (the lower jaw is shown separately). They meet at immovable joints "
                   "called sutures.",
                   ["cranium", "skull bones", "brain box", "face bones", "khopdi", "खोपड़ी"]),
        PartRecipe("mandible", "Lower jaw (mandible)", ["FMA52748"], SKULL_BONE,
                   "The only bone of the skull that moves, at a hinge joint in front of each ear, "
                   "for chewing and speaking.",
                   ["mandible", "jaw", "jaw bone", "lower jaw bone", "jabda", "जबड़ा"]),
        PartRecipe("hyoid", "Hyoid bone", ["FMA52749"], BONE,
                   "A small U-shaped bone in the neck below the jaw. It touches no other bone; "
                   "the tongue's muscles are attached to it.",
                   ["hyoid"]),
        PartRecipe("cervical_vertebrae", "Neck vertebrae (cervical, 7)",
                   ["FMA12519", "FMA12520", "FMA12521", "FMA12522", "FMA12523", "FMA12524",
                    "FMA12525"], CERVICAL,
                   "The seven vertebrae of the neck. The first two, the atlas and the axis, form "
                   "the pivot joint that lets the head turn from side to side.",
                   ["cervical vertebrae", "neck bones", "neck vertebrae", "atlas",
                    "atlas and axis"]),
        PartRecipe("thoracic_vertebrae", "Chest vertebrae (thoracic, 12)",
                   ["FMA9165", "FMA9187", "FMA9209", "FMA9248", "FMA9922", "FMA9945",
                    "FMA9968", "FMA9991", "FMA10014", "FMA10037", "FMA10059", "FMA10081"], THORACIC,
                   "The twelve vertebrae of the chest. Each pair of ribs is joined to one of them "
                   "at the back.",
                   ["thoracic vertebrae", "chest vertebrae", "dorsal vertebrae"]),
        PartRecipe("lumbar_vertebrae", "Lower back vertebrae (lumbar, 5)",
                   ["FMA13072", "FMA13073", "FMA13074", "FMA13075", "FMA13076"], LUMBAR,
                   "The five largest vertebrae, in the lower back. They carry the weight of the "
                   "whole upper body.",
                   ["lumbar vertebrae", "lower back", "lower back bones"]),
        PartRecipe("sacrum", "Sacrum", ["FMA16202"], SACRAL,
                   "Five vertebrae fused into one triangular bone at the base of the spine. It "
                   "joins the two hip bones to make the pelvis.",
                   ["sacral vertebrae", "sacral bone"]),
        PartRecipe("intervertebral_discs", "Discs between the vertebrae (cartilage)",
                   ["FMA10446", "FMA10458", "FMA13495", "FMA13500", "FMA13501", "FMA13502",
                    "FMA13503", "FMA13504", "FMA13505", "FMA13506", "FMA13507", "FMA13508",
                    "FMA13896", "FMA13897", "FMA13898", "FMA13899", "FMA13900", "FMA16033",
                    "FMA16034", "FMA16035", "FMA16036", "FMA16037", "FMA25058"], CARTILAGE,
                   "Pads of cartilage between the vertebrae. They cushion the spine and let it "
                   "bend, a little at each joint.",
                   ["discs", "intervertebral discs", "intervertebral disc", "spinal discs",
                    "vertebral discs", "disc", "disk", "disks"]),
        PartRecipe("ribs", "Ribs (12 pairs)",
                   ["FMA7857", "FMA7882", "FMA7909", "FMA7957", "FMA7987", "FMA8012", "FMA8039",
                    "FMA8066", "FMA8093", "FMA8148", "FMA8175", "FMA8202", "FMA8229", "FMA8256",
                    "FMA8283", "FMA8310", "FMA8364", "FMA8391", "FMA8445", "FMA8472", "FMA8531",
                    "FMA8532", "FMA8533", "FMA8534"], BONE,
                   "Twelve pairs of curved bones that protect the heart and lungs. The top seven "
                   "pairs join the breastbone directly (true ribs), the next three join it through "
                   "cartilage (false ribs), and the last two are free at the front (floating ribs).",
                   ["rib", "true ribs", "false ribs", "floating ribs", "pasli", "पसली", "पसलियाँ"]),
        PartRecipe("sternum", "Breastbone (sternum)", ["FMA7486", "FMA7487", "FMA7488"], BONE,
                   "The flat bone at the front of the chest, where the ribs meet. It is made of "
                   "three pieces: the manubrium, the body and the xiphoid process.",
                   ["sternum", "breast bone", "chest bone", "manubrium", "xiphoid process"]),
        PartRecipe("costal_cartilages", "Rib cartilages (costal)",
                   ["FMA7875", "FMA7886", "FMA7913", "FMA7976", "FMA8005", "FMA8031", "FMA8058",
                    "FMA8070", "FMA8112", "FMA8167", "FMA8194", "FMA8221", "FMA8248", "FMA8275"],
                   CARTILAGE,
                   "Bars of cartilage joining the ribs to the breastbone. Being flexible, they let "
                   "the chest rise and fall when we breathe.",
                   ["costal cartilages", "costal cartilage", "rib cartilage"]),
        PartRecipe("clavicle", f"Collarbone (clavicle) {_R}", ["FMA13322", "FMA13323"], BONE,
                   "A long S-shaped bone across the top of the chest, from the breastbone to the "
                   "shoulder. It holds the arm out from the body.",
                   ["clavicle", "clavicles", "collar bone", "collarbones"]),
        PartRecipe("scapula", f"Shoulder blade (scapula) {_R}", ["FMA13395", "FMA13396"], BONE,
                   "A flat triangular bone at the back of the shoulder. Its shallow socket takes "
                   "the head of the humerus at the shoulder's ball-and-socket joint.",
                   ["scapula", "scapulae", "shoulder blades", "shoulder bone"]),
        PartRecipe("humerus", f"Upper arm bone (humerus) {_R}", ["FMA23130", "FMA23131"], BONE,
                   "The long bone of the upper arm, from the shoulder to the elbow.",
                   ["upper arm bone", "arm bone", "humerus bone"]),
        PartRecipe("radius", f"Radius {_R}", ["FMA23464", "FMA23465"], BONE,
                   "The forearm bone on the thumb side. It rolls round the ulna to turn the palm "
                   "up or down.",
                   ["radius bone"]),
        PartRecipe("ulna", f"Ulna {_R}", ["FMA23467", "FMA23468"], BONE,
                   "The forearm bone on the little-finger side. Its hooked top end makes the "
                   "point of the elbow and the elbow's hinge joint.",
                   ["ulna bone", "elbow bone"]),
        PartRecipe("carpals", f"Wrist bones (carpals, 8 each) {_R}",
                   ["FMA23725", "FMA24435", "FMA24436", "FMA24437", "FMA24438", "FMA24439",
                    "FMA24440", "FMA24441", "FMA24442", "FMA24443", "FMA24444", "FMA24445",
                    "FMA24446", "FMA24447", "FMA24448", "FMA24449"], BONE,
                   "Eight small bones in two rows at the wrist, which glide over one another so "
                   "the wrist can bend and turn.",
                   ["carpals", "carpal bones", "wrist", "wrist bones", "kalai", "कलाई"]),
        PartRecipe("metacarpals", f"Palm bones (metacarpals, 5 each) {_R}",
                   ["FMA24464", "FMA24465", "FMA24466", "FMA24467", "FMA24468", "FMA24469",
                    "FMA24470", "FMA24471", "FMA24472", "FMA24473"], BONE,
                   "Five long bones in the palm, one leading to each finger.",
                   ["metacarpals", "metacarpal bones", "palm", "palm bones"]),
        PartRecipe("hand_phalanges", f"Finger bones (phalanges, 14 each) {_R}",
                   ["FMA23938", "FMA23940", "FMA23942", "FMA23944", "FMA23951", "FMA23953",
                    "FMA23955", "FMA23957", "FMA23959", "FMA24450", "FMA24451", "FMA24452",
                    "FMA24453", "FMA24454", "FMA24455", "FMA24456", "FMA24457", "FMA24458",
                    "FMA24459", "FMA24460", "FMA24461", "FMA24462", "FMA24463", "FMA65470",
                    "FMA66791", "FMA71908", "FMA71915", "FMA71916"], BONE,
                   "Three bones in each finger and two in the thumb, joined by hinge joints.",
                   ["finger bones", "fingers", "phalanges of the hand", "thumb", "ungliyan",
                    "उँगलियाँ"]),
        PartRecipe("hip_bone", f"Hip bone (pelvic girdle) {_R}", ["FMA16586", "FMA16587"], BONE,
                   "Each hip bone is three bones fused together (ilium, ischium and pubis). With "
                   "the sacrum they make the pelvis, which holds up the organs of the abdomen. Its "
                   "deep socket takes the head of the femur at the hip's ball-and-socket joint.",
                   ["hip bones", "hip", "hips", "pelvic girdle", "pelvis", "pelvic bone",
                    "ilium", "ischium", "pubis", "coxal bone"]),
        PartRecipe("femur", f"Thigh bone (femur) {_R}", ["FMA24474", "FMA24475"], BONE,
                   "The longest and strongest bone in the body, from the hip to the knee.",
                   ["thigh bone", "thighbone", "femur bone", "jaangh ki haddi"]),
        PartRecipe("patella", f"Kneecap (patella) {_R}", ["FMA24486", "FMA24487"], BONE,
                   "A small flat bone at the front of the knee, inside the tendon of the thigh "
                   "muscle. It protects the knee joint.",
                   ["kneecap", "knee cap", "knee bone", "patellae"]),
        PartRecipe("tibia", f"Shin bone (tibia) {_R}", ["FMA24477", "FMA24478"], BONE,
                   "The larger bone of the lower leg, on the inner side. It carries the body's "
                   "weight from the knee to the ankle.",
                   ["shin bone", "shinbone", "shin"]),
        PartRecipe("fibula", f"Fibula {_R}", ["FMA24480", "FMA24481"], BONE,
                   "The thin bone on the outer side of the lower leg. It carries little weight; "
                   "muscles are attached to it, and its lower end makes the outer bump of the ankle.",
                   ["fibula bone", "calf bone"]),
        PartRecipe("tarsals", f"Ankle and heel bones (tarsals, 7 each) {_R}",
                   ["FMA24482", "FMA24483", "FMA24497", "FMA24498", "FMA24500", "FMA24501",
                    "FMA24521", "FMA24522", "FMA24523", "FMA24524", "FMA24525", "FMA24526",
                    "FMA24528", "FMA24529"], BONE,
                   "Seven bones at the ankle and the back of the foot. The largest, the calcaneus, "
                   "is the heel; the talus joins the foot to the leg.",
                   ["tarsals", "tarsal bones", "ankle", "ankle bones", "heel", "heel bone",
                    "calcaneus", "talus"]),
        PartRecipe("metatarsals", f"Foot bones (metatarsals, 5 each) {_R}",
                   ["FMA24507", "FMA24508", "FMA24509", "FMA24510", "FMA24511", "FMA24512",
                    "FMA24513", "FMA24514", "FMA24515", "FMA24516"], BONE,
                   "Five long bones in the middle of the foot, one leading to each toe. With the "
                   "tarsals they make the arch of the foot.",
                   ["metatarsals", "metatarsal bones", "sole bones"]),
        PartRecipe("foot_phalanges", f"Toe bones (phalanges, 14 each) {_R}",
                   ["FMA32634", "FMA32635", "FMA32636", "FMA32637", "FMA32638", "FMA32639",
                    "FMA32640", "FMA32641", "FMA32642", "FMA32643", "FMA32644", "FMA32645",
                    "FMA32646", "FMA32647", "FMA32650", "FMA32651", "FMA32652", "FMA32653",
                    "FMA32654", "FMA32655", "FMA32656", "FMA32657", "FMA32658", "FMA32659",
                    "FMA43253", "FMA43254", "FMA230986", "FMA230988"], BONE,
                   "Three bones in each toe and two in the big toe.",
                   ["toe bones", "toes", "phalanges of the foot", "big toe"]),
    ],
    unavailable={
        "coccyx": {
            "name": "Tailbone (coccyx)",
            "aliases": ["tailbone", "tail bone", "coccygeal vertebrae"],
            "note": "BodyParts3D does not model the coccyx, the four tiny fused vertebrae below the "
                    "sacrum. It can be explained but not shown.",
        },
        "ear_ossicles": {
            "name": "Ear bones (ossicles)",
            "aliases": ["ossicles", "ear bones", "malleus", "incus", "stapes", "stirrup",
                        "hammer", "anvil"],
            "note": "The three tiny bones of each middle ear (hammer, anvil, stirrup) are not in "
                    "BodyParts3D. The stapes is the smallest bone in the body, about 3 mm long.",
        },
    },
    groups={
        "skull": ["cranium", "mandible"],
        "vertebral_column": ["cervical_vertebrae", "thoracic_vertebrae", "lumbar_vertebrae",
                             "sacrum", "intervertebral_discs"],
        "vertebrae": ["cervical_vertebrae", "thoracic_vertebrae", "lumbar_vertebrae"],
        "rib_cage": ["ribs", "sternum", "costal_cartilages", "thoracic_vertebrae"],
        "cartilage": ["intervertebral_discs", "costal_cartilages"],
        "pectoral_girdle": ["clavicle", "scapula"],
        "arm": ["humerus", "radius", "ulna", "carpals", "metacarpals", "hand_phalanges"],
        "forearm": ["radius", "ulna"],
        "hand": ["carpals", "metacarpals", "hand_phalanges"],
        "pelvis": ["hip_bone", "sacrum"],
        "leg": ["femur", "patella", "tibia", "fibula", "tarsals", "metatarsals", "foot_phalanges"],
        "lower_leg": ["tibia", "fibula"],
        "foot": ["tarsals", "metatarsals", "foot_phalanges"],
        "long_bones": ["humerus", "radius", "ulna", "femur", "tibia", "fibula"],
        "axial_skeleton": ["cranium", "mandible", "hyoid", "cervical_vertebrae",
                           "thoracic_vertebrae", "lumbar_vertebrae", "sacrum",
                           "intervertebral_discs", "ribs", "sternum", "costal_cartilages"],
        "appendicular_skeleton": ["clavicle", "scapula", "humerus", "radius", "ulna", "carpals",
                                  "metacarpals", "hand_phalanges", "hip_bone", "femur", "patella",
                                  "tibia", "fibula", "tarsals", "metatarsals", "foot_phalanges"],
    },
    group_aliases={
        "skull": ["the skull", "head bones", "khopdi", "खोपड़ी"],
        "vertebral_column": ["backbone", "spine", "spinal column", "back bone", "reedh ki haddi",
                             "रीढ़ की हड्डी", "मेरुदंड"],
        "vertebrae": ["vertebra"],
        "rib_cage": ["ribcage", "chest cage", "thoracic cage", "pinjar", "पसलियों का पिंजरा"],
        "pectoral_girdle": ["shoulder girdle", "shoulder", "shoulders", "shoulder bones"],
        "arm": ["arms", "upper limb", "upper limbs", "arm bones", "haath", "हाथ", "बाँह"],
        "hand": ["hands", "hand bones", "haath ki haddiyan"],
        "leg": ["legs", "lower limb", "lower limbs", "leg bones", "पैर", "टाँग"],
        "lower_leg": ["shin and calf"],
        "foot": ["feet", "foot bones", "पंजा"],
        "axial_skeleton": ["axial", "axial skeleton"],
        "appendicular_skeleton": ["appendicular", "limbs", "limb bones", "girdles and limbs"],
    },
    aliases=["skeleton", "human skeleton", "the skeleton", "skeletal system",
             "human skeletal system", "bones of the body", "human bones", "full skeleton",
             "whole skeleton", "bones", "kankal", "कंकाल", "कंकाल तंत्र", "asthi tantra",
             "अस्थि तंत्र", "haddiyan", "हड्डियाँ", "हड्डियां"],
    topics=["human skeleton", "skeletal system", "bones", "joints", "body movements",
            "locomotion and movement"],
    grades=["6", "7", "8", "9", "10", "11", "12"],
    relationships={"parent": "biology.anatomy.human_body"},
    notes=["199 of the 206 bones of an adult: BodyParts3D has no coccyx and no ear ossicles. "
           "The backbone's regions are tinted apart and cartilage is shown bluish-white, a "
           "teaching convention; real bone is all one colour."],
    scale_level="system",
    # 239 meshes: at the usual 30,000 the skull crumples (5% of its triangles
    # kept). 120,000 keeps it whole and still turns at ~95 fps on the Pi 5.
    low_lod_cells=120_000,
    # 16 of the 27, one per region, so the figure reads like the textbook's.
    default_labels=["cranium", "mandible", "clavicle", "scapula", "ribs", "sternum",
                    "humerus", "radius", "ulna", "hand_phalanges", "thoracic_vertebrae",
                    "hip_bone", "femur", "patella", "tibia", "foot_phalanges"],
)

# The lobes in the colours textbook brain figures use; real brain tissue is
# pinkish-grey on the outside ("grey matter") whatever the lobe.
FRONTAL, PARIETAL, TEMPORAL, OCCIPITAL = "#5b8def", "#f2c14e", "#5fbf77", "#e8697d"
DEEP, STEM, FLUID = "#d8c8e8", "#d9a066", "#7fd0f0"
BRAIN_FRONT = "+x"

BRAIN = ModelRecipe(
    model_id="biology.anatomy.brain",
    name="Human Brain",
    root_concept="FMA50801",
    parts=[
        # The fluid spaces first: the cerebral aqueduct is listed under the
        # midbrain as well, and belongs with the ventricles here.
        PartRecipe("ventricles", "Ventricles (fluid-filled spaces)", ["FMA242787"], FLUID,
                   "Four connected spaces deep in the brain, filled with cerebrospinal fluid. The "
                   "fluid cushions the brain and carries away waste.",
                   ["ventricle", "brain ventricles", "cerebrospinal fluid", "csf", "lateral ventricles",
                    "third ventricle", "fourth ventricle"]),
        PartRecipe("frontal_lobe", "Frontal lobe", ["FMA72969", "FMA72970"], FRONTAL,
                   "The front of the cerebrum. It plans, decides, controls behaviour and speech, and "
                   "its back strip (the motor cortex) starts voluntary movements.",
                   ["frontal lobes", "front of the brain", "motor cortex", "precentral gyrus", "prefrontal"]),
        PartRecipe("parietal_lobe", "Parietal lobe", ["FMA72973", "FMA72974"], PARIETAL,
                   "The top-back of the cerebrum. It receives touch, pain, heat and body position "
                   "from the skin and muscles (the sensory cortex) and works out where things are.",
                   ["parietal lobes", "sensory cortex", "postcentral gyrus"]),
        PartRecipe("occipital_lobe", "Occipital lobe", ["FMA72975", "FMA72976"], OCCIPITAL,
                   "The back of the cerebrum. It receives signals from the eyes and makes sense of "
                   "what we see.",
                   ["occipital lobes", "visual cortex", "back of the brain"]),
        PartRecipe("temporal_lobe", "Temporal lobe", ["FMA72971", "FMA72972"], TEMPORAL,
                   "The side of the cerebrum, near the ears. It handles hearing, understanding "
                   "speech, and forming memories.",
                   ["temporal lobes", "auditory cortex", "side of the brain"]),
        PartRecipe("insula", "Insula", ["FMA72977", "FMA72978"], "#c08be0",
                   "A lobe hidden deep in the fold between the frontal and temporal lobes; it "
                   "senses the body's inside state, such as hunger and heartbeat.",
                   ["insular cortex", "insular lobe"]),
        PartRecipe("limbic", "Cingulate gyrus and hippocampus (limbic)", ["FMA72980", "FMA72981"], "#f59ec4",
                   "Inner parts of the cerebrum, round its middle. The hippocampus forms new "
                   "memories; with the cingulate gyrus they are part of the limbic system, "
                   "involved in emotion.",
                   ["hippocampus", "cingulate gyrus", "limbic system", "limbic lobe"]),
        PartRecipe("inner_cerebrum", "Inner cerebrum (white matter, basal nuclei)",
                   ["FMA242184", "FMA242186", "FMA72906", "FMA72907"], DEEP,
                   "Under the folded surface: nerve fibres (white matter) joining the parts of the "
                   "brain, and groups of nerve cells deep inside (the basal nuclei).",
                   ["white matter", "internal capsule", "basal ganglia", "basal nuclei", "subcortex"]),
        PartRecipe("hypothalamus", "Hypothalamus", ["FMA62008"], "#f08a5d",
                   "A small region at the base of the forebrain that controls body temperature, "
                   "hunger, thirst and sleep, and tells the pituitary gland what to do.",
                   []),
        PartRecipe("pineal", "Pineal gland", ["FMA62009"], "#9fe0a8",
                   "A tiny gland in the middle of the brain that makes melatonin, the hormone that "
                   "helps set the sleep-wake cycle.",
                   ["pineal body", "epithalamus", "habenula"]),
        PartRecipe("pituitary", "Pituitary gland", ["FMA13889"], "#ff8fb3",
                   "A pea-sized gland hanging below the hypothalamus. Its hormones control growth "
                   "and many other glands, which is why it is called the master gland.",
                   ["pituitary", "master gland", "hypophysis"]),
        PartRecipe("midbrain", "Midbrain", ["FMA61993"], "#e6b37a",
                   "The top of the brainstem. It relays signals for seeing and hearing and helps "
                   "control eye movements and reflexes.",
                   ["mid brain", "mesencephalon", "colliculus", "colliculi"]),
        PartRecipe("pons", "Pons", ["FMA67943"], STEM,
                   "The bulge in the brainstem below the midbrain. It links the cerebrum with the "
                   "cerebellum and helps control breathing.",
                   []),
        PartRecipe("medulla", "Medulla oblongata", ["FMA62004"], "#c98a5a",
                   "The lowest part of the brainstem, joining the spinal cord. It controls "
                   "involuntary actions: heartbeat, breathing, blood pressure, swallowing, vomiting.",
                   ["medulla oblongata", "medulla"]),
        PartRecipe("cerebellum", "Cerebellum", ["FMA67944"], "#b07cd8",
                   "The 'little brain' at the back, under the cerebrum. It keeps balance and "
                   "posture and makes movements smooth and precise.",
                   ["little brain", "hindbrain cerebellum"]),
        PartRecipe("spinal_cord", "Spinal cord (top)", ["FMA7647"], "#e9d8a6",
                   "The bundle of nerves running down from the medulla inside the backbone, "
                   "carrying messages between the brain and the body. Shown: its first few "
                   "centimetres.",
                   ["spinal cord", "spine cord"], keep_z_above=35.0,
                   note="Trimmed to the 35 mm below the brain; the source continues down the back."),
    ],
    unavailable={
        "corpus_callosum": {
            "name": "Corpus callosum",
            "aliases": ["corpus callosum"],
            "note": "BodyParts3D does not model the corpus callosum, the thick band of fibres "
                    "joining the two hemispheres, separately; here it is part of the inner cerebrum.",
        },
        "thalamus": {
            "name": "Thalamus",
            "aliases": ["thalamus"],
            "note": "BodyParts3D does not model the thalamus separately; it lies within the inner "
                    "cerebrum here. It can be explained but not highlighted.",
        },
    },
    groups={
        "cerebrum": ["frontal_lobe", "parietal_lobe", "occipital_lobe", "temporal_lobe", "insula",
                     "limbic", "inner_cerebrum"],
        "lobes": ["frontal_lobe", "parietal_lobe", "occipital_lobe", "temporal_lobe"],
        "forebrain": ["frontal_lobe", "parietal_lobe", "occipital_lobe", "temporal_lobe", "insula",
                      "limbic", "inner_cerebrum", "hypothalamus", "pineal"],
        "hindbrain": ["pons", "medulla", "cerebellum"],
        "brainstem": ["midbrain", "pons", "medulla"],
        "glands": ["pituitary", "pineal", "hypothalamus"],
    },
    group_aliases={
        "cerebrum": ["cerebral cortex", "cortex", "big brain", "cerebral hemispheres", "hemispheres",
                     "grey matter", "gray matter"],
        "lobes": ["four lobes", "the lobes", "brain lobes"],
        "forebrain": ["fore brain", "prosencephalon"],
        "hindbrain": ["hind brain", "rhombencephalon"],
        "brainstem": ["brain stem", "stem"],
        "glands": ["endocrine glands", "hormone glands"],
    },
    aliases=["brain", "human brain", "the brain", "my brain", "dimag", "dimaag", "दिमाग", "दिमाग़",
             "मस्तिष्क", "mastishk", "brain structure", "parts of the brain"],
    topics=["human brain", "nervous system", "control and coordination", "neural control and coordination",
            "parts of the brain"],
    grades=["6", "7", "8", "9", "10", "11", "12"],
    relationships={"parent": "biology.anatomy.human_body"},
    notes=["Lobes are coloured as in textbook figures; real brain tissue is pinkish-grey outside "
           "and creamy white inside.",
           "BodyParts3D has no superior temporal gyrus, the top fold of the temporal lobe, so the "
           "insula shows through between the frontal and temporal lobes."],
    # What can be seen from the side; the ventricles, insula, pineal gland,
    # hypothalamus, midbrain and inner cerebrum are inside it and are named
    # when asked for.
    default_labels=["frontal_lobe", "parietal_lobe", "occipital_lobe", "temporal_lobe", "cerebellum",
                    "pons", "medulla", "spinal_cord", "pituitary"],
    front=BRAIN_FRONT,
    # At the usual 30,000 the folds went blocky (9% kept); 100,000 keeps them.
    low_lod_cells=100_000,
)

RECIPES = {HEART.model_id: HEART, SKELETON.model_id: SKELETON, BRAIN.model_id: BRAIN}


def assign_elements(recipe: ModelRecipe, elements: dict[str, set[str]]) -> dict[str, list[str]]:
    """Part id -> element ids, first claim wins. Raises if a part ends up empty."""
    taken: set[str] = set()
    out: dict[str, list[str]] = {}
    for part in recipe.parts:
        if part.remainder_of:
            wanted = set(elements.get(part.remainder_of, ()))
        else:
            wanted = set().union(*(elements.get(c, set()) for c in part.concepts)) if part.concepts else set()
        for c in part.minus:
            wanted -= elements.get(c, set())
        wanted -= taken
        if not wanted:
            raise ValueError(f"{recipe.model_id}: part {part.id} has no source geometry")
        taken |= wanted
        out[part.id] = sorted(wanted)
    return out


def all_concepts(recipe: ModelRecipe) -> list[str]:
    ids = {recipe.root_concept}
    for part in recipe.parts:
        ids.update(part.concepts)
        ids.update(part.minus)
    return sorted(ids)


class HeartbeatAnimator:
    """An EDUCATIONAL heartbeat: atria, then ventricles, contract in turn.

    The timing follows the cardiac cycle at 72 beats per minute (atrial
    systole ~0.1 s, then ventricular systole ~0.3 s), but the motion is a
    uniform scaling of each chamber about its own centre -- a teaching cue,
    not a simulation of how heart muscle deforms.
    """

    name = "heartbeat"
    period_s = 60.0 / 72.0

    def __init__(self) -> None:
        self.t = 0.0

    def _scale(self, phase: float, start: float, length: float, depth: float) -> float:
        x = (phase - start) / length
        return 1.0 - depth * math.sin(math.pi * x) if 0.0 <= x <= 1.0 else 1.0

    def tick(self, dt: float, engine) -> None:
        self.t += dt
        phase = self.t % self.period_s
        atria = self._scale(phase, 0.0, 0.1, 0.06)
        ventricles = self._scale(phase, 0.12, 0.3, 0.08)
        for part_id, s in (("right_atrium", atria), ("left_atrium", atria),
                           ("right_ventricle", ventricles), ("left_ventricle", ventricles)):
            engine.scale_part(part_id, s)

    def reset(self, engine) -> None:
        self.t = 0.0
        for part_id in ("right_atrium", "left_atrium", "right_ventricle", "left_ventricle"):
            engine.scale_part(part_id, 1.0)

