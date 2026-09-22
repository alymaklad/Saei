"""
Term normalization and requirement/evidence matching.

This module exists because of a specific, measured failure in the original
scorer. Keyword matching was:

    matched = sum(1 for s in required_skills if s.lower() in cv_lower)

Plain substring containment, which has two failure modes that were both live
in this project's database:

  FALSE POSITIVES. "R" is in the extracted requirements for a real job here.
  `"r" in cv_lower` is true for every CV ever written. So are "Vector",
  "Git" (matches "GitHub", "legitimate", "digit") and "math" (matches
  "automatic"? no -- but "mathematics", "aftermath", "mathematician" yes).
  These silently inflate the score, which is worse than the false negatives:
  they make the system apply to jobs it shouldn't.

  FALSE NEGATIVES. No aliasing at all, so "RESTful APIs" in the job
  description never matches "REST API development" in the CV.

Both are fixed here: matching is word-boundary-aware (with the boundary class
extended so C++, C#, .NET and Node.js survive), and equivalence goes through
a curated table.

The harder question this module answers
---------------------------------------
"RESTful APIs" == "REST API" is easy. The interesting cases are:

    requirement "Deep Learning", evidence "CNN"          -> satisfied
    requirement "AWS",           evidence "Google Cloud" -> NOT satisfied

Embedding cosine similarity cannot separate these. It will score AWS/GCP HIGH
precisely because they are the same category -- comparably high to
CNN/Deep Learning. Any threshold that rejects the first also rejects the
second. The distinction is not one of degree but of direction:

    CNN  subset-of  Deep Learning   (evidence entails the requirement)
    GCP  sibling-of AWS             (neither implies the other)

Cosine similarity is symmetric and therefore structurally incapable of
expressing that. So embeddings are used only to RETRIEVE candidate CV spans
worth examining (see agents/ats_agent.py); the verdict comes from HYPONYMS
below, which is directional by construction, or from an LLM asked the
directional question explicitly.

EXCLUSIVE_GROUPS is the backstop: members of the same group never satisfy one
another, and are never even sent to the LLM. A model that talks itself into
"GCP shows cloud experience, close enough" cannot do damage here.
"""
import re
import unicodedata

# ---- normalization ----------------------------------------------------------

# Characters that count as "inside a term" for boundary purposes. Deliberately
# wider than \w: without '+' the pattern for "C++" would match the "C" in
# "C++", and without '.' or '#' the same problem hits ".NET" and "C#".
_TERM_CHARS = r"A-Za-z0-9+#._"

_PUNCT_STRIP = re.compile(r"^[^A-Za-z0-9+#.]+|[^A-Za-z0-9+#]+$")
_WS = re.compile(r"\s+")


# Typography the writer never chose. A rewrite comes back from the model with
# U+2011 non-breaking hyphens and curly apostrophes -- "tool‑calling",
# "Bachelor’s" -- and every one of them is invisible on the page and fatal to a
# comparison against text typed with an ASCII hyphen. It cost a real run ten
# points: the entailment pass quoted its evidence back with a plain hyphen,
# _evidence_is_real could not find that quote in the CV it came from, and four
# confirmed verdicts were discarded as fabrications.
#
# Folded 1:1 so string offsets are preserved -- find_term_with_context slices
# the ORIGINAL text with offsets found in the folded copy, and a fold that
# changed lengths would quietly shift every snippet.
_TYPOGRAPHY = {
    **{ord(c): "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d"},
    ord("\u00a0"): " ", ord("\u2007"): " ", ord("\u202f"): " ",
    ord("\u2018"): "'", ord("\u2019"): "'", ord("\u02bc"): "'",
    ord("\u201c"): '"', ord("\u201d"): '"',
}


def fold_typography(text: str) -> str:
    """Unicode dashes, spaces and quotes to their ASCII equivalents, 1:1."""
    return text.translate(_TYPOGRAPHY) if text else text


def normalize(term: str) -> str:
    """Lowercase, strip accents and edge punctuation, collapse whitespace.

    Keeps '+', '#' and '.' because they are load-bearing in real skill names
    (C++, C#, .NET, Node.js) -- stripping them would collapse C and C++ into
    the same token, which is precisely the kind of silent equivalence this
    module is built to prevent.
    """
    if not term:
        return ""
    text = unicodedata.normalize("NFKD", fold_typography(term))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = _WS.sub(" ", text).strip().lower()
    text = _PUNCT_STRIP.sub("", text)
    return text


def _plural_variants(canon: str) -> list[str]:
    """'API' <-> 'APIs'. Cheap and safe; a real stemmer would over-merge
    (e.g. 'Java' and 'JavaScript' are distinct and a stemmer that merged them
    would be a disaster here).

    Words ending -is/-us/-ss are left alone: 'analysis' is not the plural of
    'analysi', and generating that garbage form put a nonsense token into the
    search set for every requirement mentioning analysis.
    """
    out = [canon]
    if canon.endswith("s") and len(canon) > 3 and not canon.endswith(("is", "us", "ss")):
        out.append(canon[:-1])
    elif not canon.endswith("s"):
        out.append(canon + "s")
    return out


# ---- equivalence: different words, same thing --------------------------------
#
# canonical -> surface forms that MEAN THE SAME THING. Bidirectional: seeing
# any of these in the CV satisfies a requirement for any other, at full credit.
#
# STRICTLY synonyms and abbreviations. Anything broader/narrower belongs in
# HYPONYMS, which is directional and pays partial credit. The distinction is
# not pedantry -- an entry here is an assertion that the two terms are
# interchangeable in BOTH directions, so putting "LoRA" here as an alias of
# PEFT would mean a job requiring LoRA specifically is satisfied by a CV that
# only says "PEFT". That is a false equivalence, the same failure class as
# AWS/GCP, just less obvious.
#
# Entries removed from this table and moved to HYPONYMS, with the reason:
#   lora, qlora        -> narrower techniques WITHIN parameter-efficient
#                         fine-tuning, not names for it
#   multimodal model   -> broader than vision-language model (audio, video)
#   foundation model   -> broader than LLM (also covers vision/multimodal)
#   git                -> one version control system, not the concept
#   data structures,   -> each is HALF of "data structures and algorithms";
#   algorithms            neither alone is the same subject
#   ci, cd             -> distinct practices; "CI/CD" is the pair, so
#                         evidence of one is partial support for the compound
#   analytical thinking-> overlaps problem solving without being it
#   cross-functional   -> a specific kind of collaboration
#     collaboration

ALIASES: dict[str, list[str]] = {
    "javascript": ["js", "ecmascript"],
    "typescript": ["ts"],
    "python": ["python3", "py"],
    "postgresql": ["postgres", "psql"],
    "microsoft sql server": ["mssql", "sql server", "t-sql", "tsql"],
    "mongodb": ["mongo"],
    # "es" dropped: a two-letter token that is a common word in several
    # languages, and this project scrapes mixed-language MENA boards.
    "elasticsearch": ["elastic search"],
    "kubernetes": ["k8s"],
    "continuous integration": ["ci"],
    "continuous deployment": ["cd", "continuous delivery"],
    "ci/cd": ["ci cd", "cicd"],
    "rest api": ["restful api", "restful apis", "rest apis", "restful", "rest",
                 "rest api development", "restful web services", "rest services"],
    "graphql": ["graph ql"],
    "machine learning": ["ml"],
    "deep learning": ["dl"],
    "natural language processing": ["nlp"],
    # "cv" is deliberately NOT an alias for computer vision here. It reads as
    # one in a job posting and as the document itself in a CV -- and this app
    # scores CVs, whose project and bullet text says "CV" constantly. Reading
    # the skills back out of a real profile turned "Job Application Agent,
    # Autonomous Job ... CV ..." into demonstrated computer-vision work. The
    # cost of dropping it is a posting that writes "CV" for the field and a
    # candidate who never spells it out; the cost of keeping it is every
    # résumé matching.
    "computer vision": ["computer-vision"],
    "large language model": ["llm", "llms", "large language models"],
    # Naming variants of one field, not degrees of it. "Built an autonomous
    # multi-agent system" and "delivers agentic AI solutions" are the same
    # claim in two vocabularies, and treating them as related-but-lesser sent
    # a perfect match to the LLM entailment pass, which caps at 0.75.
    # Degrees. A posting writes "Bachelor's degree"; a CV writes "Dual Bachelor
    # Degree" or "BSc". Aly's own education line missed the requirement
    # entirely on the apostrophe and was rescued by the LLM at 0.75 -- a
    # degree that is plainly on the page should never need a model's opinion.
    "bachelor's degree": ["bachelor degree", "bachelors degree", "bachelors",
                          "bachelor's", "bachelor of science", "bachelor of engineering",
                          "bachelor of arts", "bsc", "b.sc", "b.s.", "bs degree",
                          "beng", "ba degree", "undergraduate degree"],
    "master's degree": ["master degree", "masters degree", "masters", "master's",
                        "master of science", "master of engineering", "msc", "m.sc",
                        "m.s.", "ms degree", "meng", "postgraduate degree"],
    "phd": ["ph.d", "ph.d.", "doctorate", "doctoral degree", "dphil"],
    # Categories the AI stack is described in. Each of these was a
    # `semantic_support` row on a real posting -- the LLM catching what the
    # tables did not know, at a 0.75 cap that punishes the tables rather than
    # the candidate.
    "vector database": ["vector databases", "vector store", "vector stores",
                        "vector db", "vector search engine"],
    "agent framework": ["agent frameworks", "agentic framework", "agentic frameworks",
                        "ai orchestration framework", "agent orchestration framework",
                        "multi-agent framework"],
    "llm api": ["llm apis", "llm api integration", "llm provider api",
                "foundation model api"],
    "full-stack development": ["full stack development", "fullstack development",
                               "full-stack engineering", "full stack engineering"],
    "ai engineering": ["artificial intelligence engineering", "ai systems engineering"],
    "agentic ai": ["agentic artificial intelligence", "agentic ai solutions",
                   "agentic systems", "agentic system", "agentic workflows",
                   "agentic workflow", "ai agents", "ai agent", "autonomous agents",
                   "autonomous agent", "multi-agent system", "multi agent system",
                   "multi-agent systems", "multiagent system", "agent systems",
                   "agent workflows"],
    "foundation model": ["foundation models"],
    "retrieval augmented generation": ["rag", "retrieval-augmented generation"],
    "amazon web services": ["aws"],
    "google cloud platform": ["gcp", "google cloud"],
    "microsoft azure": ["azure"],
    "mathematics": ["math", "maths"],
    "statistics": ["statistical analysis", "stats"],
    "version control": ["source control", "source code management", "scm"],
    "object oriented programming": ["oop", "object-oriented programming"],
    "object relational mapping": ["orm"],
    "user interface": ["ui"],
    "user experience": ["ux"],
    "extract transform load": ["etl"],
    "business intelligence": ["bi"],
    "test driven development": ["tdd"],
    # Scrum IS a kind of agile and Docker IS a way to containerize, so both
    # belong in HYPONYMS (directional), not here. Listing them as equivalences
    # would also satisfy a "Docker" requirement from the bare word
    # "containers" anywhere in the CV, which is exactly the loose matching
    # this module replaces.
    "agile": ["agile methodology", "agile methodologies"],
    "data structures and algorithms": ["dsa"],
    "reinforcement learning": ["rl"],
    "generative ai": ["genai", "gen ai", "generative artificial intelligence"],
    "artificial intelligence": ["ai"],
    "convolutional neural network": ["cnn", "convnet", "convolutional neural networks"],
    "recurrent neural network": ["rnn", "recurrent neural networks"],
    "vision transformer": ["vit"],
    "supervised fine tuning": ["sft", "supervised fine-tuning"],
    "parameter efficient fine tuning": ["peft", "parameter-efficient fine-tuning"],
    "vision language model": ["vlm", "vision-language model"],
    "multimodal model": ["multi-modal model", "multimodal models"],
    "communication skills": ["communication", "verbal communication",
                             "written communication", "strong communication"],
    "problem solving": ["problem-solving"],
    # "collaboration" deliberately NOT here. It is a reasonable synonym in
    # isolation, but every narrower phrase that mentions it -- "cross-functional
    # collaboration", "collaboration with product teams" -- would then match as
    # an alias at full credit, because alias matching runs before the
    # hierarchy is consulted and wins. It lives in HYPONYMS instead, where it
    # earns partial credit like every other narrower term. See
    # test_alias_surface_forms_do_not_shadow_hyponyms.
    "teamwork": ["team player", "collaborative"],
}

# surface form -> canonical
_SURFACE_TO_CANON: dict[str, str] = {}
for _canon, _forms in ALIASES.items():
    _SURFACE_TO_CANON[normalize(_canon)] = normalize(_canon)
    for _f in _forms:
        _SURFACE_TO_CANON[normalize(_f)] = normalize(_canon)


# ---- entailment: specific evidence satisfies a general requirement -----------
#
# requirement canonical -> evidence canonicals that DEMONSTRATE it.
#
# DIRECTIONAL ON PURPOSE. "CNN" in the CV satisfies a "Deep Learning"
# requirement, because a CNN is a kind of deep learning. The reverse is not
# true: "Deep Learning" on a CV does not demonstrate the specific CNN
# experience a job asking for CNNs wants. Encoding this as a dict keyed by
# requirement, never consulted in reverse, is what makes the asymmetry
# structural rather than a matter of tuning.

HYPONYMS: dict[str, list[str]] = {
    "deep learning": [
        "convolutional neural network", "recurrent neural network", "transformer",
        "transformers", "lstm", "gan", "resnet", "bert", "vision transformer",
        "large language model", "neural network", "neural networks", "u-net", "unet",
        "diffusion model", "vision language model",
    ],
    "machine learning": [
        "deep learning", "convolutional neural network", "random forest", "xgboost",
        "gradient boosting", "svm", "scikit-learn", "sklearn", "large language model",
        "reinforcement learning", "supervised learning", "unsupervised learning",
        "classification", "regression", "clustering",
    ],
    "artificial intelligence": [
        "machine learning", "deep learning", "natural language processing",
        "computer vision", "large language model", "generative ai",
    ],
    "computer vision": [
        "image classification", "object detection", "image segmentation",
        "semantic segmentation", "convolutional neural network", "opencv",
        "medical imaging", "vision transformer", "image processing",
        # Sub-fields a CV names instead of the field. Their absence is why a
        # graduation project reading "YOLO tracking, MediaPipe pose estimation
        # and CTR-GCN motion classification" left a Computer Vision
        # requirement earning skills-list credit.
        "pose estimation", "object tracking", "image captioning",
        "optical character recognition", "face recognition", "face detection",
        "visual question answering", "video analytics", "image retrieval",
        "image generation", "depth estimation", "action recognition",
    ],
    "natural language processing": [
        "large language model", "transformer", "transformers", "bert", "named entity recognition",
        "sentiment analysis", "text classification", "tokenization", "spacy", "nltk",
    ],
    "generative ai": [
        "large language model", "diffusion model", "gan", "retrieval augmented generation",
        "prompt engineering", "vision language model",
    ],
    "deep learning framework": ["pytorch", "tensorflow", "keras", "jax"],
    "cloud platform": ["amazon web services", "google cloud platform", "microsoft azure"],
    "cloud computing": ["amazon web services", "google cloud platform", "microsoft azure"],
    "container orchestration": ["kubernetes", "docker swarm", "ecs"],
    "containerization": ["docker", "podman", "kubernetes", "containers"],
    # "agile methodology" deliberately absent -- it is an ALIAS of agile, and
    # listing it here too would leave the code choosing between two credits
    # for one term.
    "agile": ["scrum", "kanban", "sprint planning", "extreme programming"],
    "relational database": [
        "postgresql", "mysql", "microsoft sql server", "sqlite", "oracle database",
        "mariadb", "sqlalchemy", "object relational mapping",
    ],
    # ORMs included deliberately, at partial credit. Using SQLAlchemy is real
    # relational-database work, but an ORM can also abstract the SQL away
    # entirely -- so it supports a SQL requirement without proving it, which is
    # exactly what "subset" credit means. Note the token rules stop
    # "SQLAlchemy" from matching a bare "SQL" requirement directly: the 'A'
    # after "sql" is a term character, so there is no word boundary. That is
    # correct (SQLite, SQLAlchemy and MySQL are not the SQL language) and is
    # why the relationship has to be stated here instead.
    "sql": ["postgresql", "mysql", "microsoft sql server", "sqlite", "mariadb",
            "sqlalchemy", "object relational mapping", "stored procedures",
            "query optimization", "window functions"],
    "object relational mapping": ["sqlalchemy", "hibernate", "prisma", "django orm",
                                  "entity framework", "sqlmodel", "typeorm",
                                  "active record"],
    "nosql": ["mongodb", "cassandra", "dynamodb", "redis", "couchdb", "neo4j"],
    "vector database": ["pinecone", "weaviate", "chroma", "chromadb", "faiss", "qdrant", "milvus"],
    "database": ["postgresql", "mysql", "mongodb", "sqlite", "microsoft sql server", "redis"],
    "web framework": ["django", "flask", "fastapi", "express", "spring boot", "rails"],
    "frontend framework": ["react", "angular", "vue", "svelte", "next.js"],
    "programming language": [
        "python", "java", "javascript", "c++", "c#", "go", "rust", "typescript", "ruby",
    ],
    "data visualization": ["matplotlib", "seaborn", "plotly", "tableau", "power bi", "d3.js"],
    "data analysis": ["pandas", "numpy", "statistics", "sql", "exploratory data analysis"],
    "mlops": ["mlflow", "kubeflow", "dvc", "weights and biases", "wandb", "model deployment"],
    "ci/cd": ["github actions", "gitlab ci", "jenkins", "circleci", "travis ci"],
    "continuous integration": ["github actions", "gitlab ci", "jenkins", "circleci"],
    "version control": ["git", "github", "gitlab", "bitbucket", "svn"],
    "agent framework": ["langchain", "langgraph", "llamaindex", "autogen", "crewai"],
    "llm framework": ["langchain", "langgraph", "llamaindex", "autogen"],
    "operating system": ["linux", "unix", "windows", "macos"],
    "linux": ["ubuntu", "debian", "centos", "red hat", "fedora"],

    # ---- moved out of ALIASES, where they were false equivalences ----------

    # LoRA and QLoRA are techniques within PEFT. Requiring PEFT is satisfied by
    # having done LoRA; requiring LoRA specifically is NOT satisfied by "PEFT".
    "parameter efficient fine tuning": [
        "lora", "qlora", "adapter tuning", "adapters", "prefix tuning",
        "prompt tuning", "ia3",
    ],
    "fine tuning": ["parameter efficient fine tuning", "supervised fine tuning",
                    "lora", "qlora", "instruction tuning", "rlhf", "dpo"],

    # A vision-language model is one kind of multimodal model; audio and video
    # models are others. So VLM evidence supports a multimodal requirement, but
    # a job asking specifically for VLM work is not satisfied by "multimodal".
    "multimodal model": ["vision language model", "audio language model",
                         "video language model", "clip", "flamingo"],

    # "Foundation model" spans language, vision and multimodal; LLM is the
    # language subset.
    "foundation model": ["large language model", "vision language model",
                         "multimodal model", "diffusion model"],

    # Git is one VCS among several -- already listed under version control
    # above, kept there rather than duplicated.

    # Each half of DSA is genuinely half. Evidence of one supports the compound
    # requirement at partial credit rather than satisfying it outright.
    "data structures and algorithms": [
        "data structures", "algorithms", "algorithm design", "complexity analysis",
        "leetcode", "competitive programming",
    ],

    # CI and CD are distinct practices; "CI/CD" names the pair. Evidence of
    # either is partial support for the compound.
    "ci/cd": ["continuous integration", "continuous deployment",
              "github actions", "gitlab ci", "jenkins", "circleci"],

    # Overlapping but not identical soft skills.
    "problem solving": ["analytical thinking", "analytical skills",
                        "critical thinking", "troubleshooting", "debugging"],
    "teamwork": ["collaboration", "cross-functional collaboration",
                 "pair programming", "code review", "mentoring"],
}

_HYPONYMS_NORM: dict[str, set[str]] = {
    normalize(req): {normalize(e) for e in evs} for req, evs in HYPONYMS.items()
}


# ---- prerequisite: you could not have done X without doing Y ------------------
#
# requirement canonical -> evidence canonicals whose USE REQUIRES it.
#
# A third relation, and the distinction from HYPONYMS is the whole reason it
# exists. FastAPI is not a KIND OF Python the way a CNN is a kind of deep
# learning -- it is a Python framework, so someone who built a FastAPI service
# WROTE PYTHON. The candidate did not do something adjacent to the
# requirement; they did the requirement, while calling it something else.
#
# That is why an implication scores above a subset (config.RELATION_CREDIT)
# and why it counts as DEMONSTRATED when the evidence sits in a dated role: a
# skills-list entry saying "Python" is a claim, but a bullet saying "built a
# FastAPI service" is proof of the same thing.
#
# The bar for an entry here is that the implication is definitional, not
# probable. "Used Kubernetes" probably means containers, but a platform team
# can inherit manifests; "used Django" cannot mean anything but Python. When
# in doubt it belongs in HYPONYMS at subset credit, or nowhere.
#
# Kept out deliberately:
#   docker      <- kubernetes    orchestrating containers someone else built
#   linux       <- docker        Docker Desktop on Windows and macOS is normal
#   machine learning <- pandas   data wrangling is not modelling
#   rest api    <- fastapi       you can serve GraphQL or gRPC from it

IMPLIED_BY: dict[str, list[str]] = {
    "python": [
        "fastapi", "django", "flask", "pytorch", "tensorflow", "keras", "jax",
        "pandas", "numpy", "scikit-learn", "sklearn", "scipy", "matplotlib",
        "seaborn", "langchain", "langgraph", "llamaindex", "streamlit",
        "gradio", "celery", "sqlalchemy", "pydantic", "huggingface",
        "transformers", "jupyter", "pyspark", "airflow", "opencv-python",
        "qlora", "peft",
    ],
    "javascript": [
        "react", "angular", "vue", "svelte", "next.js", "nuxt", "node.js",
        "express", "typescript", "jquery", "redux", "webpack", "vite",
    ],
    "typescript": ["angular", "nest.js", "nestjs"],
    "java": ["spring", "spring boot", "hibernate", "maven", "gradle", "junit"],
    "c#": [".net", "asp.net", "entity framework", "blazor"],
    "php": ["laravel", "symfony", "wordpress"],
    "ruby": ["rails", "ruby on rails"],
    "dart": ["flutter"],
    # Querying any of these means writing SQL. HYPONYMS already calls them a
    # KIND OF SQL; this says the stronger and more useful thing -- the person
    # wrote queries.
    "sql": [
        "postgresql", "mysql", "microsoft sql server", "sqlite", "mariadb",
        "oracle database", "bigquery", "redshift", "snowflake",
    ],
    "amazon web services": [
        "s3", "ec2", "lambda", "dynamodb", "sagemaker", "cloudformation",
        "eks", "rds", "cloudwatch",
    ],
    "google cloud platform": ["bigquery", "vertex ai", "gke", "cloud run"],
    "microsoft azure": ["azure devops", "azure ml", "aks", "azure functions"],
    "git": ["github", "gitlab", "bitbucket", "github actions"],
    "html": ["css"],
    "linux": ["bash", "shell scripting"],
    "deep learning": ["pytorch", "tensorflow", "keras", "jax", "qlora", "peft"],
    # Models and toolkits nobody touches without doing the field itself.
    # Listed here rather than as hyponyms because YOLO is not a KIND of
    # computer vision, it is a thing you cannot use without doing it.
    "computer vision": ["yolo", "mediapipe", "opencv", "vision transformer",
                        "clip", "resnet", "vision-language model",
                        "vision language model", "detectron", "torchvision"],
    "agentic ai": ["langgraph", "autogen", "crewai", "react agent",
                   "tool calling", "agent orchestrator", "multi-agent orchestrator"],
    "object detection": ["yolo", "detectron", "faster r-cnn"],
    "vector database": ["qdrant", "pinecone", "weaviate", "chroma", "chromadb",
                        "faiss", "milvus", "pgvector"],
    "agent framework": ["langgraph", "autogen", "crewai", "semantic kernel"],
    "llm api": ["openai", "anthropic", "gemini", "groq", "openrouter",
                "langchain", "llamaindex"],
    "ai engineering": ["langchain", "langgraph", "llamaindex", "qlora",
                       "retrieval augmented generation", "fine-tuning",
                       "large language model"],
    "pose estimation": ["mediapipe"],
    "machine learning": ["scikit-learn", "sklearn", "xgboost", "lightgbm"],
}

_IMPLIED_BY_NORM: dict[str, set[str]] = {
    normalize(requirement): {normalize(e) for e in evidence}
    for requirement, evidence in IMPLIED_BY.items()
}


# ---- non-substitutability ----------------------------------------------------
#
# Members of the same group are SIBLINGS: same category, mutually exclusive as
# evidence. This is the guard the user's review specifically asked for --
# "AWS != GCP" -- generalized. Checked BEFORE the LLM ever sees a pair, so no
# amount of model creativity can decide that Google Cloud demonstrates AWS
# experience.
#
# Note these are checked on canonical forms, so "Google Cloud" and "GCP"
# collapse to the same member first.

EXCLUSIVE_GROUPS: list[set[str]] = [
    {"amazon web services", "google cloud platform", "microsoft azure",
     "oracle cloud", "ibm cloud", "alibaba cloud", "digitalocean", "heroku"},
    {"pytorch", "tensorflow", "jax", "mxnet", "paddlepaddle", "caffe", "theano"},
    {"python", "java", "javascript", "c++", "c#", "go", "rust", "ruby", "php",
     "scala", "kotlin", "swift", "r", "matlab", "perl", "c"},
    {"react", "angular", "vue", "svelte", "ember"},
    {"django", "flask", "fastapi", "express", "spring boot", "rails", "laravel"},
    {"postgresql", "mysql", "microsoft sql server", "oracle database", "sqlite", "mariadb"},
    {"mongodb", "cassandra", "dynamodb", "couchdb", "neo4j"},
    {"kubernetes", "docker swarm", "nomad", "mesos"},
    {"github actions", "gitlab ci", "jenkins", "circleci", "travis ci"},
    {"windows", "linux", "macos"},
]

_MEMBER_TO_GROUP: dict[str, int] = {}
for _i, _group in enumerate(EXCLUSIVE_GROUPS):
    for _m in _group:
        _MEMBER_TO_GROUP[normalize(_m)] = _i


# ---- generic packaging words -------------------------------------------------
#
# Job descriptions wrap skills in filler: "RAG systems", "Python programming",
# "CI/CD pipelines", "experience with Kubernetes". The skill is the same one;
# the extra word carries no requirement of its own. Left unreduced, every such
# phrase misses the tables entirely and falls through to the LLM, which costs a
# call and scores it as semantic support (0.75) instead of a direct hit (1.00)
# -- so the CV gets marked down for the phrasing of the posting.

_GENERIC_HEADS = (
    "systems", "system", "platforms", "platform", "technologies", "technology",
    "tools", "tooling", "stack", "services", "solutions", "pipelines", "pipeline",
    "programming", "experience", "knowledge",
    "expertise", "proficiency", "skills", "concepts", "principles", "practices",
    "methodologies", "environments", "ecosystem", "workflows", "workflow",
)
# "engineering" and "development" are deliberately NOT heads. They look
# generic but form real compound skills where the head carries meaning:
# prompt engineering, feature engineering, data engineering, test-driven
# development. Stripping "engineering" reduced "prompt engineering" to
# "prompt", which stopped it being recognised as a kind of generative AI --
# caught by the evaluation set, not by inspection.

_LEADING_QUALIFIERS = (
    "experience with", "experience in", "experience using", "hands on with",
    "hands-on with", "hands on", "hands-on", "working knowledge of",
    "knowledge of", "proficiency in", "proficiency with", "familiarity with",
    "expertise in", "understanding of", "background in", "exposure to",
    "strong", "solid", "deep", "proven", "demonstrated", "advanced", "basic",
)


def _is_known(term: str) -> bool:
    """Is this a term any of the tables recognises?"""
    return (term in _SURFACE_TO_CANON
            or term in _HYPONYMS_NORM
            or term in _MEMBER_TO_GROUP)


def is_known(term: str) -> bool:
    """Does any table recognise this term, in any of its spellings?

    The public form of _is_known, which takes an already-normalised string and
    is easy to call wrongly from outside: `_is_known("Deep Learning")` is
    False and `_is_known("deep learning")` is True, which is a difference no
    caller should have to know about. Anything asking "is this in the
    vocabulary?" -- requirement_normalizer decides whether a requirement is a
    named skill or a capability on exactly that -- should call this.
    """
    if not term:
        return False
    return (_is_known(normalize(term))
            or _is_known(canonical(term))
            or bool(_IMPLIED_BY_NORM.get(canonical(term))))


def _reduce_to_known_core(norm: str) -> str | None:
    """Strip generic packaging, but ONLY when what's left is a term the tables
    already know.

    That condition is what makes this safe. "Deep learning framework" would
    reduce to "deep learning" -- a real term, but the wrong one: the framework
    requirement means PyTorch or TensorFlow, not the field. So a phrase that is
    ITSELF known is never touched, which protects every multi-word key in
    HYPONYMS ("web framework", "vector database", "relational database",
    "deep learning framework") from being collapsed into its head noun.
    """
    if _is_known(norm):
        return None

    candidate = norm
    for prefix in _LEADING_QUALIFIERS:
        if candidate.startswith(prefix + " "):
            candidate = candidate[len(prefix) + 1:].strip()
            break

    words = candidate.split()
    while len(words) > 1 and words[-1] in _GENERIC_HEADS:
        words = words[:-1]
    candidate = " ".join(words)

    if candidate == norm or not candidate:
        return None
    if _is_known(candidate):
        return candidate
    for variant in _plural_variants(candidate):
        if _is_known(variant):
            return variant

    # The tables can't list every tool on earth ("Docker tooling",
    # "Terraform experience"), so an unrecognised remainder is still usually
    # the real skill -- unless it is one of these, which name whole fields
    # rather than anything checkable. "Software development" reduced to
    # "software" would match the word anywhere and mean nothing.
    if candidate in _TOO_GENERIC_ALONE or len(candidate) < 3:
        return None
    return candidate


_TOO_GENERIC_ALONE = {
    "software", "systems", "system", "data", "web", "cloud", "technical",
    "business", "product", "project", "design", "research", "computer",
    "information", "digital", "application", "applications", "code", "coding",
    "programming", "analysis", "analytics", "management", "operations",
}


def canonical(term: str) -> str:
    """Map a term to its canonical form, following the alias table.

    Unknown terms normalize but pass through unchanged -- the table is a set
    of known equivalences, not a whitelist of allowed skills.
    """
    norm = normalize(term)
    if norm in _SURFACE_TO_CANON:
        return _SURFACE_TO_CANON[norm]
    for variant in _plural_variants(norm):
        if variant in _SURFACE_TO_CANON:
            return _SURFACE_TO_CANON[variant]

    core = _reduce_to_known_core(norm)
    if core:
        return _SURFACE_TO_CANON.get(core, core)
    return norm


# A posting names an ACTIVITY, a CV names the ROLE that does it: "full-stack
# development" against "Full-Stack Developer". They are the same claim, and
# the difference is a suffix -- so it is handled by a rule on the last word
# rather than by an alias row for every "<something> development" a posting
# can invent. Only the last token is rewritten, so "software development
# lifecycle" is untouched.
_ROLE_SUFFIXES = {
    "development": ("developer", "developers", "developing"),
    "developer": ("development", "developers"),
    "engineering": ("engineer", "engineers"),
    "engineer": ("engineering", "engineers"),
    "administration": ("administrator", "administrators", "admin"),
    "management": ("manager", "managers", "managing"),
    "analysis": ("analyst", "analysts", "analytics"),
    "design": ("designer", "designers"),
    "testing": ("tester", "testers"),
    "programming": ("programmer", "programmers"),
    "architecture": ("architect", "architects"),
}


def _role_variants(canon: str) -> set[str]:
    words = canon.split()
    if len(words) < 2:
        # A bare "development" or "engineering" is too generic to rewrite:
        # every CV contains one of them somewhere.
        return set()
    swaps = _ROLE_SUFFIXES.get(words[-1], ())
    return {" ".join(words[:-1] + [swap]) for swap in swaps}


def surface_forms(term: str) -> list[str]:
    """Every string worth searching the CV for, given a requirement.

    Longest first, so a match reports the most specific form it found
    ("machine learning" rather than "ml") in the evidence shown to the user.
    """
    canon = canonical(term)
    forms = {normalize(term), canon}
    forms.update(normalize(f) for f in ALIASES.get(canon, []))
    forms.update(_plural_variants(canon))
    forms.update(_role_variants(canon))
    for alias in ALIASES.get(canon, []):
        forms.update(_role_variants(normalize(alias)))
    forms.discard("")
    return sorted(forms, key=len, reverse=True)


def are_mutually_exclusive(a: str, b: str) -> bool:
    """True when two terms are siblings in the same category -- same kind of
    thing, but one is not evidence of the other. AWS vs GCP; PyTorch vs
    TensorFlow; Python vs Java."""
    ca, cb = canonical(a), canonical(b)
    if ca == cb:
        return False
    ga, gb = _MEMBER_TO_GROUP.get(ca), _MEMBER_TO_GROUP.get(cb)
    return ga is not None and ga == gb


def entails(requirement: str, evidence: str) -> bool:
    """Does `evidence` demonstrate `requirement`?

    Deliberately NOT symmetric: entails("deep learning", "cnn") is True,
    entails("cnn", "deep learning") is False.
    """
    req, ev = canonical(requirement), canonical(evidence)
    if req == ev:
        return True
    if are_mutually_exclusive(req, ev):
        return False

    # _HYPONYMS_NORM is keyed on normalize(), which is all that's available
    # while the module is still being built. canonical() can map a term
    # somewhere else entirely, so check both forms -- otherwise a table entry
    # silently stops matching the moment normalization rules change, which is
    # exactly how "prompt engineering" disappeared once "engineering" became a
    # strippable head.
    known = _HYPONYMS_NORM.get(req) or _HYPONYMS_NORM.get(normalize(requirement), set())
    return ev in known or normalize(evidence) in known


def implying_terms(requirement: str) -> list[str]:
    """Evidence terms whose use would have required `requirement`.

    The counterpart of HYPONYMS.get() for the prerequisite relation, filtered
    through the same sibling guard so a table typo cannot make one member of
    an exclusive group imply another.
    """
    canon = canonical(requirement)
    candidates = _IMPLIED_BY_NORM.get(canon) or _IMPLIED_BY_NORM.get(normalize(requirement), set())
    return sorted(term for term in candidates if not are_mutually_exclusive(canon, term))


def implied_by(requirement: str, evidence: str) -> bool:
    """Does using `evidence` require `requirement`?

    Directional, like entails(), and for the same reason: writing FastAPI
    means writing Python, but writing Python does not mean writing FastAPI.
    """
    req, ev = canonical(requirement), canonical(evidence)
    if req == ev or are_mutually_exclusive(req, ev):
        return False
    known = _IMPLIED_BY_NORM.get(req) or _IMPLIED_BY_NORM.get(normalize(requirement), set())
    return ev in known or normalize(evidence) in known


_PATTERN_CACHE: dict[str, re.Pattern] = {}


def _pattern(form: str) -> re.Pattern:
    """Word-boundary regex for one surface form.

    Uses explicit lookarounds rather than \\b because \\b is defined against
    \\w, which excludes '+', '#' and '.' -- so r"\\bC\\+\\+\\b" fails to match
    "C++" at end of line, and r"\\bC\\b" happily matches the C inside "C++".

    '.' is treated ASYMMETRICALLY on the trailing side: it only counts as
    term-internal when an alphanumeric follows it. Otherwise "Git." at the end
    of a sentence would fail to match "Git" -- a false negative every bit as
    damaging as the false positives this module was written to kill -- while
    "Node.js" would still wrongly satisfy a bare "Node" requirement.
    """
    form = fold_typography(form)
    if form not in _PATTERN_CACHE:
        _PATTERN_CACHE[form] = re.compile(
            rf"(?<![{_TERM_CHARS}]){re.escape(form)}(?![A-Za-z0-9+#_]|\.[A-Za-z0-9])",
            re.IGNORECASE,
        )
    return _PATTERN_CACHE[form]


# ---- homographs: the same letters, a different skill ------------------------
#
# Word boundaries fixed "R" matching inside "Tarek". They do nothing about a
# term that IS a whole word somewhere else in the field's own vocabulary. The
# case that found this: "Developed a LangGraph ReAct agent" scored a React
# requirement at Exact -- and then, because React is evidence of JavaScript,
# awarded a JavaScript row too, on a CV with no frontend work anywhere in it.
# ReAct is Reason+Act, a prompting pattern; React is a UI library; the strings
# differ only in one capital letter that nobody can rely on.
#
# So each entry is a canonical term plus the contexts in which those letters
# mean something else. A hit inside one of these is not a hit -- the scan
# moves on and looks for another occurrence, because a CV that says both
# "ReAct agent" and "React frontend" should still score React.
#
# Kept deliberately small. This is not a place to encode taste; a phrase
# belongs here only when the same spelling names a genuinely different thing,
# and the alternative is inventing a skill the candidate never claimed.
FALSE_FRIENDS: dict[str, list[str]] = {
    "react": [
        # ReAct: reasoning-and-acting agents. "React agent" is never the
        # library -- nobody calls a component an agent.
        r"react[\s-]+(agent|agents|style|pattern|loop|prompt|prompting|framework\b(?![\s-]*(app|ui|frontend)))",
        r"(langgraph|langchain|llamaindex|autogen|crewai)[\s-]+react",
        # "a react-based LangGraph ReAct agent" -- a real tailored CV wrote
        # this. React-based anything else is left alone.
        r"react[\s-]*based[\s-]+(langgraph|langchain|llamaindex|autogen|crewai|agent)",
        r"reasoning[\s-]+and[\s-]+acting",
    ],
    "go": [
        # The language, against the ordinary verb it is spelled like.
        r"go[\s-]+to[\s-]+market", r"go[\s-]+live", r"go[\s-]+beyond",
    ],
}

_FALSE_FRIENDS_RE: dict[str, list[re.Pattern]] = {
    normalize(term): [re.compile(p, re.IGNORECASE) for p in patterns]
    for term, patterns in FALSE_FRIENDS.items()
}


def _is_false_friend(canon: str, text: str, start: int, end: int) -> bool:
    """Does this particular occurrence sit inside a phrase that means something
    else? Overlap, not containment: the match has to be part of the phrase, so
    "ReAct" inside "ReAct agent" is rejected while a "React" three sentences
    away is untouched."""
    for pattern in _FALSE_FRIENDS_RE.get(canon, ()):
        for bad in pattern.finditer(text):
            if bad.start() < end and start < bad.end():
                return True
    return False


def _search(form: str, text: str, canon: str) -> re.Match | None:
    """First occurrence of `form` that is really `canon` and not its homograph.

    Searches a typography-folded copy, whose offsets are the original's, so a
    caller can still quote the text the way the candidate wrote it.
    """
    text = fold_typography(text)
    for match in _pattern(form).finditer(text):
        if not _is_false_friend(canon, text, match.start(), match.end()):
            return match
    return None


def find_term(term: str, text: str) -> str | None:
    """The surface form of `term` actually present in `text`, or None.

    This is the function that replaces `s.lower() in cv_lower`. Under the old
    rule, find_term("R", "Aly Tarek") was a match; here the 'r' in "Tarek" is
    preceded by a term character, so it isn't.
    """
    if not term or not text:
        return None
    canon = normalize(canonical(term))
    for form in surface_forms(term):
        if _search(form, text, canon):
            return form
    return None


def find_term_with_context(term: str, text: str, window: int = 90) -> tuple[str, str] | None:
    """Like find_term, but also returns the surrounding text as evidence.

    Scores in this project are required to carry the reason attached, not just
    a number -- a bare "matched: PyTorch" is not auditable, whereas "Trained
    ResNet50 using PyTorch" is.
    """
    if not term or not text:
        return None
    canon = normalize(canonical(term))
    for form in surface_forms(term):
        match = _search(form, text, canon)
        if match:
            start = max(0, match.start() - window // 2)
            end = min(len(text), match.end() + window // 2)
            snippet = _WS.sub(" ", text[start:end]).strip()
            if start > 0:
                snippet = "..." + snippet
            if end < len(text):
                snippet = snippet + "..."
            return form, snippet
    return None
