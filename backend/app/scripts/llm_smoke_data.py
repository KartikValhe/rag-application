"""
app/scripts/llm_smoke_data.py
─────────────────────────────
SAMPLE material for app/scripts/llm_smoke.py (step 6a-6). Made-up teaching text about React, Angular and Redux,
in English and Marathi. It contains no real student, teacher, institution or document. Do not add real data.

Each language has:
  * 6 short source passages: three about React (props, state, hooks), one about React components, and the
    related-but-different Angular routing and Redux passages, so near-topic traps exist;
  * 20 questions, interleaved in blocks of five (answerable x3, near-topic, unrelated) so that any `--count`
    still has every kind:
      answerable   the sources contain the answer (expected first line ANSWERABLE);
      near_topic   about a related topic the sources do NOT cover (guard, Toolkit, useReducer, Input decorator):
                   expected NOT_IN_SOURCES;
      unrelated    nothing to do with the sources: expected NOT_IN_SOURCES.

The Marathi is plain written Marathi with the framework terms kept in Latin letters, as in real course notes.
The owner reads the answers; no Marathi quality score is computed anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

SAMPLE_NOTICE: Final = "SAMPLE data for llm_smoke: made-up text, no real student or institution."

Kind = Literal["answerable", "near_topic", "unrelated"]
Verdict = Literal["ANSWERABLE", "NOT_IN_SOURCES"]


@dataclass(frozen=True, slots=True)
class Passage:
    title: str
    location: str
    section: str
    text: str


@dataclass(frozen=True, slots=True)
class Question:
    id: str
    kind: Kind
    text: str

    @property
    def expected(self) -> Verdict:
        return "ANSWERABLE" if self.kind == "answerable" else "NOT_IN_SOURCES"


PASSAGES: Final[dict[str, tuple[Passage, ...]]] = {
    "en": (
        Passage(
            "React props",
            "SAMPLE slide 1",
            "Components > Props",
            "Props are the inputs a parent component passes to a child component. They are written like attributes "
            "in JSX. The child reads them from its props object. Props are read-only: a child component must not "
            "change the props it receives.",
        ),
        Passage(
            "React state",
            "SAMPLE slide 2",
            "Components > State",
            "State is data that a component owns and that can change over time. The useState hook returns the "
            "current value and a setter function. Calling the setter function makes the component render again "
            "with the new value.",
        ),
        Passage(
            "React hooks: useEffect",
            "SAMPLE slide 3",
            "Hooks > useEffect",
            "The useEffect hook runs code after the component has rendered. It is used for outside work such as "
            "fetching data or subscribing to events. The dependency array decides when the effect runs again; an "
            "empty array makes it run only once, after the first render. A cleanup function returned by the effect "
            "runs before the effect runs again and when the component is removed.",
        ),
        Passage(
            "React components and JSX",
            "SAMPLE slide 4",
            "Components > JSX",
            "A React component is a function that returns JSX, a syntax that looks like HTML. The name of a "
            "component always starts with a capital letter. One component can contain other components.",
        ),
        Passage(
            "Angular routing",
            "SAMPLE slide 5",
            "Angular > Routing",
            "The Angular Router connects URL paths to components. The routes are written in an array, each with a "
            "path and a component. The router-outlet shows the component that matches the current URL, and the "
            "routerLink directive moves to another route without reloading the page.",
        ),
        Passage(
            "Redux basics",
            "SAMPLE slide 6",
            "Redux > Store",
            "Redux keeps the whole application state in one store. A component sends an action, which is a plain "
            "object with a type. A reducer is a pure function that takes the old state and the action and returns "
            "the new state.",
        ),
    ),
    "mr": (
        Passage(
            "React मधील Props",
            "SAMPLE स्लाइड 1",
            "घटक > Props",
            "Props म्हणजे पालक घटकाकडून बाल घटकाला पाठवलेली माहिती. ती JSX मध्ये गुणधर्मासारखी लिहिली जाते. "
            "बाल घटक props फक्त वाचू शकतो; तो त्यात बदल करू शकत नाही, कारण props वाचनीय (read-only) असतात.",
        ),
        Passage(
            "React मधील State",
            "SAMPLE स्लाइड 2",
            "घटक > State",
            "State म्हणजे घटकाच्या मालकीची माहिती, जी वेळेनुसार बदलू शकते. useState ही hook सध्याचे मूल्य आणि "
            "ते बदलण्यासाठी एक setter फंक्शन देते. setter फंक्शन बोलावल्यावर घटक नवीन मूल्यासह पुन्हा रेंडर होतो.",
        ),
        Passage(
            "React Hooks: useEffect",
            "SAMPLE स्लाइड 3",
            "Hooks > useEffect",
            "useEffect घटक रेंडर झाल्यानंतर कोड चालवतो; माहिती आणणे किंवा घटनांची सदस्यता घेणे अशा बाह्य कामांसाठी "
            "तो वापरतात. dependency array ठरवतो की effect पुन्हा कधी चालेल; रिकामा array असल्यास तो पहिल्या "
            "रेंडरनंतर फक्त एकदाच चालतो. effect ने परत केलेले cleanup फंक्शन पुढच्या वेळी चालण्यापूर्वी आणि घटक "
            "काढला जाताना चालते.",
        ),
        Passage(
            "React घटक आणि JSX",
            "SAMPLE स्लाइड 4",
            "घटक > JSX",
            "React मधील घटक हे एक फंक्शन असते जे JSX परत करते. JSX ची रचना HTML सारखी दिसते. घटकाचे नाव नेहमी "
            "मोठ्या अक्षराने सुरू होते. एका घटकात इतर घटक ठेवता येतात.",
        ),
        Passage(
            "Angular मधील Routing",
            "SAMPLE स्लाइड 5",
            "Angular > Routing",
            "Angular चा Router URL मार्गांना घटकांशी जोडतो. मार्ग एका array मध्ये path आणि component यांसह लिहिले "
            "जातात. router-outlet सध्याच्या URL शी जुळलेला घटक दाखवतो, आणि routerLink पान पुन्हा लोड न करता "
            "दुसऱ्या मार्गावर नेतो.",
        ),
        Passage(
            "Redux ची मूलतत्त्वे",
            "SAMPLE स्लाइड 6",
            "Redux > Store",
            "Redux अॅप्लिकेशनची सर्व state एकाच store मध्ये ठेवतो. घटक action पाठवतो; action म्हणजे type असलेली "
            "साधी वस्तू. reducer हे असे शुद्ध फंक्शन आहे जे जुनी state आणि action घेऊन नवीन state परत करते.",
        ),
    ),
}


def _build(lang: str, rows: tuple[tuple[Kind, str], ...]) -> tuple[Question, ...]:
    return tuple(Question(f"{lang}-{number:02d}", kind, text) for number, (kind, text) in enumerate(rows, start=1))


QUESTIONS: Final[dict[str, tuple[Question, ...]]] = {
    "en": _build(
        "en",
        (
            ("answerable", "What is a prop in React?"),
            ("answerable", "Can a child component change the props it receives?"),
            ("answerable", "How does a parent component pass data to a child component?"),
            ("near_topic", "How do I protect an Angular route with a route guard?"),
            ("unrelated", "What is the capital of France?"),
            ("answerable", "What does the useState hook return?"),
            ("answerable", "What happens when the setter function of a state is called?"),
            ("answerable", "What does the useEffect hook do?"),
            ("near_topic", "How does createSlice of Redux Toolkit work?"),
            ("unrelated", "How do I bake bread at home?"),
            ("answerable", "How do I make an effect run only once, after the first render?"),
            ("answerable", "When does the cleanup function of an effect run?"),
            ("answerable", "What letter must the name of a React component start with?"),
            ("near_topic", "How does the useReducer hook work in React?"),
            ("unrelated", "Who won the cricket world cup in 1983?"),
            ("answerable", "What does the Angular Router connect to components?"),
            ("answerable", "What is the router-outlet used for in Angular?"),
            ("answerable", "What does a reducer do in Redux?"),
            ("near_topic", "How do I pass data between Angular components with the Input decorator?"),
            ("unrelated", "What is photosynthesis?"),
        ),
    ),
    "mr": _build(
        "mr",
        (
            ("answerable", "React मध्ये prop म्हणजे काय?"),
            ("answerable", "बाल घटक त्याला मिळालेले props बदलू शकतो का?"),
            ("answerable", "पालक घटक बाल घटकाला माहिती कशी पाठवतो?"),
            ("near_topic", "Angular मध्ये route guard ने मार्ग कसा सुरक्षित करायचा?"),
            ("unrelated", "फ्रान्सची राजधानी कोणती?"),
            ("answerable", "useState काय परत करते?"),
            ("answerable", "state चे setter फंक्शन बोलावल्यावर काय होते?"),
            ("answerable", "useEffect काय करते?"),
            ("near_topic", "Redux Toolkit मधील createSlice कसे काम करते?"),
            ("unrelated", "घरी भाकरी कशी बनवायची?"),
            ("answerable", "effect फक्त पहिल्या रेंडरनंतर एकदाच चालवण्यासाठी काय करावे?"),
            ("answerable", "effect चे cleanup फंक्शन केव्हा चालते?"),
            ("answerable", "React घटकाचे नाव कोणत्या अक्षराने सुरू व्हावे?"),
            ("near_topic", "React मध्ये useReducer hook कसे काम करते?"),
            ("unrelated", "१९८३ चा क्रिकेट विश्वचषक कोणी जिंकला?"),
            ("answerable", "Angular चा Router कशाला कशाशी जोडतो?"),
            ("answerable", "Angular मध्ये router-outlet चा उपयोग काय?"),
            ("answerable", "Redux मधील reducer काय करतो?"),
            ("near_topic", "Angular मध्ये Input decorator वापरून घटकांमध्ये माहिती कशी पाठवायची?"),
            ("unrelated", "प्रकाशसंश्लेषण म्हणजे काय?"),
        ),
    ),
}
