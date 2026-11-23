"""Continents and regions for the GDACS country filter.

GDACS has no region search, and its country filter is a substring match, so
"Africa" would match only "Central African Republic" and "South Africa". The
server searches worldwide instead and keeps the events that name a country or
place in the region.

GDACS names places in free text ("Near Coast Of Peru", "Kamchatka Peninsula",
"Off Coast Of South Africa"), so a name matches a region when it contains one
of the region's names as whole words. The longest name wins, so "Papua New
Guinea" is not read as "Guinea" and "New Ireland Region" is not "Ireland".
Spellings below are the ones GDACS used in 2019-2026.
"""

from __future__ import annotations

import re
import unicodedata


def norm(name: str) -> str:
    """Lower-case, accent-free, single-spaced form of a country name."""
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = " ".join(name.casefold().replace("’", "'").split())
    return name.removeprefix("the ")


# African sub-regions follow the UN geoscheme (M49).
_NORTH_AFRICA = [
    "Algeria", "Egypt", "Libya", "Morocco", "Sudan", "Tunisia", "Western Sahara",
    "Canary Islands",
]

_WEST_AFRICA = [
    "Benin", "Burkina Faso", "Cape Verde", "Cabo Verde", "Cote d'Ivoire", "Ivory Coast",
    "Gambia", "Ghana", "Guinea", "Guinea-Bissau", "Liberia", "Mali", "Mauritania",
    "Niger", "Nigeria", "Senegal", "Sierra Leone", "Togo", "St. Helena", "Saint Helena",
]

_EAST_AFRICA = [
    "Burundi", "Comoros", "Djibouti", "Eritrea", "Ethiopia", "Kenya", "Madagascar",
    "Malawi", "Mauritius", "Mayotte", "Mozambique", "Reunion", "La Reunion", "Rwanda",
    "Seychelles", "Somalia", "South Sudan", "Tanzania", "Uganda", "Zambia", "Zimbabwe",
]

_CENTRAL_AFRICA = [
    "Angola", "Cameroon", "Central African Republic", "Chad", "Congo",
    "Congo-Brazzaville", "Republic of Congo", "Democratic Republic of Congo",
    "Democratic Republic of the Congo", "DR Congo", "Equatorial Guinea", "Gabon",
    "Sao Tome and Principe", "Sao Tome & Principe",
]

_SOUTHERN_AFRICA = ["Botswana", "Eswatini", "Swaziland", "Lesotho", "Namibia", "South Africa"]

_AFRICA = _NORTH_AFRICA + _WEST_AFRICA + _EAST_AFRICA + _CENTRAL_AFRICA + _SOUTHERN_AFRICA

_EUROPE = [
    "Albania", "Andorra", "Austria", "Belarus", "Belgium", "Bosnia and Herzegovina",
    "Bosnia & Herzegovina", "Bulgaria", "Croatia", "Cyprus", "Czech Republic",
    "Czechia", "Denmark", "Estonia", "Faroe Islands", "Finland", "France", "Germany",
    "Gibraltar", "Greece", "Hungary", "Iceland", "Ireland", "Isle of Man", "Italy",
    "Kosovo", "Latvia", "Liechtenstein", "Lithuania", "Luxembourg", "Malta", "Moldova",
    "Monaco", "Montenegro", "Netherlands", "North Macedonia", "Norway", "Poland",
    "Portugal", "Romania", "San Marino", "Serbia", "Slovakia", "Slovenia", "Spain",
    "Sweden", "Switzerland", "Ukraine", "United Kingdom", "Great Britain", "Svalbard",
    "Jan Mayen",
    # Partly in Europe, mostly in Asia: counted in both (see TRANSCONTINENTAL).
    # Georgia, Armenia, Azerbaijan and Kazakhstan are counted in Asia only.
    "Russia", "Russian Federation", "Turkey", "Türkiye",
    # Places GDACS names instead of a country
    "Crete", "Sicily", "Corsica", "Sardinia", "Dodecanese Islands", "Azores",
    "Madeira", "Aegean Sea", "Ionian Sea", "Strait Of Gibraltar",
]

_MIDDLE_EAST = [
    "Bahrain", "Cyprus", "Egypt", "Iran", "Iraq", "Israel", "Jordan", "Kuwait",
    "Lebanon", "Oman", "Palestine", "Palestinian territories", "Qatar",
    "Saudi Arabia", "Syria", "Syrian Arab Republic", "Turkey", "Türkiye",
    "United Arab Emirates", "Yemen", "Socotra", "Arabian Peninsula",
]

_SOUTH_ASIA = [
    "Afghanistan", "Bangladesh", "Bhutan", "India", "Maldives", "Nepal", "Pakistan",
    "Sri Lanka", "Andaman Islands", "Nicobar Islands", "Hindu Kush", "Kashmir",
]

_SOUTHEAST_ASIA = [
    "Brunei", "Cambodia", "East Timor", "Timor-Leste", "Indonesia", "Laos",
    "Malaysia", "Myanmar", "Philippines", "Singapore", "Thailand", "Vietnam",
    "Viet Nam",
    # Places GDACS names instead of a country
    "Luzon", "Mindanao", "Mindoro", "Leyte", "Samar", "Panay", "Philippine Islands",
    "Java", "Sumatera", "Sumatra", "Sulawesi", "Halmahera", "Seram", "Ceram Sea",
    "Flores", "Sumba", "Sumbawa", "Bali", "Irian Jaya", "Talaud Islands",
    "Aru Islands", "Tanimbar Islands", "Minahassa Peninsula", "Molucca Sea",
    "Banda Sea", "Celebes Sea", "Java Sea", "Bali Sea", "Flores Sea", "Sunda Strait",
    "Timor",
]

_EAST_ASIA = [
    "China", "Hong Kong", "Macau", "Japan", "Mongolia", "North Korea",
    "Democratic Republic of Korea", "South Korea", "Republic of Korea", "Korea",
    "Taiwan",
    # Places GDACS names instead of a country
    "Hokkaido", "Honshu", "Kyushu", "Shikoku", "Ryukyu Islands", "Ryukyu Isl.",
    "Bonin Islands", "Volcano Islands", "Sea Of Japan", "Xizang", "Xinjiang",
    "Sichuan", "Yunnan", "Qinghai",
]

_CENTRAL_ASIA = ["Kazakhstan", "Kyrgyzstan", "Tajikistan", "Turkmenistan", "Uzbekistan"]

_ASIA = (
    [n for n in _MIDDLE_EAST if n != "Egypt"] + _SOUTH_ASIA + _SOUTHEAST_ASIA
    + _EAST_ASIA + _CENTRAL_ASIA
    + [
        "Russia", "Russian Federation", "Georgia", "Armenia", "Azerbaijan",
        "Siberia", "Lake Baykal", "Kamchatka", "Kuril Islands", "Sakhalin",
        "Komandorsky Islands", "Sea Of Okhotsk", "Caucasus",
    ]
)

_CENTRAL_AMERICA = [
    "Belize", "Costa Rica", "El Salvador", "Guatemala", "Honduras", "Nicaragua", "Panama",
    "Central America",  # GDACS: "Off Coast Of Central America"
]

_CARIBBEAN = [
    "Anguilla", "Antigua and Barbuda", "Antigua & Barbuda", "Aruba", "Bahamas",
    "Barbados", "Bonaire", "British Virgin Islands", "British Virgin Is.",
    "Cayman Islands", "Cayman Is.", "Cuba", "Curacao", "Dominica",
    "Dominican Republic", "Grenada", "Guadeloupe", "Haiti", "Jamaica", "Martinique",
    "Montserrat", "Netherlands Antilles", "Puerto Rico", "Saint Barthelemy",
    "Saint Kitts and Nevis", "Saint Lucia", "St. Lucia", "Saint Martin",
    "Sint Maarten", "Saint Vincent and the Grenadines",
    "St. Vincent & the Grenadines", "Trinidad and Tobago", "Trinidad & Tobago",
    "Turks and Caicos Islands", "Virgin Islands", "Leeward Islands",
    "Windward Islands", "Mona Passage",
]

_MEXICO = [
    "Mexico", "Baja California", "Chiapas", "Oaxaca", "Guerrero", "Jalisco",
    "Michoacan", "Gulf Of California", "Revilla Gigedo Islands",
]

_NORTHERN_AMERICA = [
    "United States", "United States of America", "USA", "U.S.", "Canada",
    "Greenland", "Bermuda", "Saint Pierre and Miquelon",
    # Places GDACS names instead of a country
    "Alaska", "Aleutian Islands", "Aleutian Is.", "Andreanof Islands", "Rat Islands",
    "Fox Islands", "Pribilof Islands", "Unimak Island", "Kodiak Island",
    "Hawaii", "California", "Nevada", "Oregon", "Idaho", "Utah", "Texas", "Oklahoma",
    "New Mexico", "Vancouver Island", "Queen Charlotte Islands",
]

_SOUTH_AMERICA = [
    "Argentina", "Arg.", "Bolivia", "Brazil", "Chile", "Colombia", "Ecuador",
    "Falkland Islands", "Falkland Is.", "French Guiana", "Guyana", "Paraguay", "Peru",
    "Suriname", "Uruguay", "Venezuela",
    # Places GDACS names instead of a country
    "Galapagos Islands", "Easter Island", "Jujuy Province", "Salta Province",
    "San Juan Province", "La Rioja Province", "Santiago Del Estero",
    "Tierra del Fuego",
]

_OCEANIA = [
    "Australia", "New Zealand", "N.Z.", "Papua New Guinea", "P.N.G.", "Png.",
    "New Guinea", "Fiji", "Solomon Islands", "Solomon Is.", "Vanuatu",
    "New Caledonia", "Samoa", "American Samoa", "Tonga", "Kiribati", "Tuvalu",
    "Nauru", "Palau", "Micronesia", "Marshall Islands", "Marshall Is.", "Guam",
    "Northern Mariana Islands", "Northern Mariana Is.", "Mariana Islands",
    "Cook Islands", "Niue", "French Polynesia", "Wallis and Futuna",
    "Wallis & Futuna", "Pitcairn", "Norfolk Island", "Tokelau",
    # Places GDACS names instead of a country
    "Gilbert Islands", "Caroline Islands", "Admiralty Islands", "New Britain",
    "New Ireland", "D'entrecasteaux Islands", "Loyalty Islands", "Santa Cruz Islands",
    "Kermadec Islands", "Auckland Islands", "North Island", "South Island",
    "Cook Strait", "Macquarie Island", "Northern Territory", "Queensland",
    "New South Wales", "Tasmania", "Western Australia", "Bismarck Sea",
]

# Region name -> the names that place an event in it.
REGIONS: dict[str, list[str]] = {
    "Africa": _AFRICA,
    "North Africa": _NORTH_AFRICA,
    "West Africa": _WEST_AFRICA,
    "East Africa": _EAST_AFRICA,
    "Central Africa": _CENTRAL_AFRICA,
    "Southern Africa": _SOUTHERN_AFRICA,
    "Europe": _EUROPE,
    "Asia": _ASIA,
    "Middle East": _MIDDLE_EAST,
    "South Asia": _SOUTH_ASIA,
    "Southeast Asia": _SOUTHEAST_ASIA,
    "East Asia": _EAST_ASIA,
    "Central Asia": _CENTRAL_ASIA,
    "North America": _NORTHERN_AMERICA + _MEXICO + _CENTRAL_AMERICA + _CARIBBEAN,
    "Central America": _CENTRAL_AMERICA,
    "Caribbean": _CARIBBEAN,
    "South America": _SOUTH_AMERICA,
    "Latin America": _MEXICO + _CENTRAL_AMERICA + _CARIBBEAN + _SOUTH_AMERICA,
    "Americas": _NORTHERN_AMERICA + _MEXICO + _CENTRAL_AMERICA + _CARIBBEAN + _SOUTH_AMERICA,
    "Oceania": _OCEANIA,
}

# What a user may type -> region name.
REGION_LOOKUP: dict[str, str] = {norm(name): name for name in REGIONS}
REGION_LOOKUP.update({
    norm(alias): region
    for alias, region in {
        "Northern Africa": "North Africa",
        "Western Africa": "West Africa",
        "Eastern Africa": "East Africa",
        "Middle Africa": "Central Africa",
        "Australasia": "Oceania",
        "the Pacific Islands": "Oceania",
        "Pacific Islands": "Oceania",
        "South-East Asia": "Southeast Asia",
        "South East Asia": "Southeast Asia",
        "the Americas": "Americas",
        "the Middle East": "Middle East",
        "the Caribbean": "Caribbean",
    }.items()
})

# Names that contain a region's name but are elsewhere: matched first (longest
# wins) so they place the event in no region, or the right one.
_ELSEWHERE = ["South Georgia", "South Sandwich Islands", "South Shetland Islands",
              "South Of Africa", "Southwest Of Africa"]

# Countries in both Europe and Asia: all their events count in both, because
# GDACS's statistic rows have no coordinates to split them by.
TRANSCONTINENTAL = {"Russia", "Turkey"}

# Seas and oceans, searched by event coordinates: (display name, box). A box
# is (west, south, east, north) in degrees, negative lon west; west > east
# crosses the 180th meridian. Approximate, and they include coasts.
_SEAS: list[tuple[list[str], tuple[float, float, float, float]]] = [
    (["Bay of Bengal"], (80, 5, 95, 23)),
    (["Arabian Sea"], (50, 0, 77, 25)),
    (["South China Sea"], (105, 0, 121, 23)),
    (["Philippine Sea"], (120, 5, 140, 35)),
    (["Coral Sea"], (145, -30, 165, -10)),
    (["Gulf of Mexico"], (-98, 18, -80, 31)),
    (["Mediterranean", "Mediterranean Sea"], (-6, 30, 36, 46)),
    (["Indian Ocean", "North Indian Ocean"], (20, -60, 100, 30)),
    (["Pacific", "Pacific Ocean", "North Pacific", "South Pacific", "Western Pacific"], (100, -60, -70, 66)),
    (["Atlantic", "Atlantic Ocean", "North Atlantic", "South Atlantic"], (-100, -60, 20, 66)),
    (["Arctic", "Arctic Ocean"], (-180, 66, 180, 90)),
]
SEA_BOXES: dict[str, tuple[str, tuple[float, float, float, float]]] = {
    norm(name): (names[0], box) for names, box in _SEAS for name in names
}

# Groupings the tool has no list for: the error asks for the countries instead.
UNSUPPORTED_REGIONS = {
    norm(name) for name in [
        "sub-saharan africa", "horn of africa", "sahel",
        "western europe", "eastern europe", "southern europe", "northern europe",
        "scandinavia", "nordic countries", "balkans", "the balkans", "baltic states",
    ]
}

# "world", "global": the same as no country.
WORLDWIDE = {norm(name) for name in ["world", "worldwide", "global", "the world", "everywhere"]}

_KEY_REGIONS: dict[str, set[str]] = {norm(n): set() for n in _ELSEWHERE}
for _region, _names in REGIONS.items():
    for _name in _names:
        _KEY_REGIONS.setdefault(norm(_name), set()).add(_region)

# One pattern, longest names first so the longest match wins; a name only
# matches as whole words.
_PATTERN = re.compile(
    r"(?<![\w])(?:"
    + "|".join(re.escape(k) for k in sorted(_KEY_REGIONS, key=len, reverse=True))
    + r")(?![\w])"
)


def regions_of(name: str) -> set[str]:
    """The regions a GDACS country or place name lies in (empty if none is known)."""
    found: set[str] = set()
    for match in _PATTERN.finditer(norm(name)):
        found |= _KEY_REGIONS[match.group(0)]
    return found


def in_region(names: list[str], region: str) -> bool:
    """True if any of an event's country or place names lies in the region."""
    return any(region in regions_of(n) for n in names)
