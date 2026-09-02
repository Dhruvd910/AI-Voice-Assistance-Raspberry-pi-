-- A starter knowledge graph: enough to answer "what came before this?" for the
-- spine of school maths and science, not an attempt at a full curriculum.
--
-- The edges are the point. min_class keeps a recommendation age-appropriate, so
-- a stuck Class 7 student is sent back to a Class 4 idea rather than sideways to
-- something merely related. Board-specific splits (NCERT/CBSE/ICSE) are the
-- fast-follow the original brief already flagged; nothing here contradicts them.
--
-- Idempotent: re-running only fills gaps, so this can be applied after an edit.

INSERT INTO concepts (slug, name, subject, min_class) VALUES
    ('counting',            'Counting and number order',        'maths',   1),
    ('addition',            'Addition',                         'maths',   1),
    ('subtraction',         'Subtraction',                      'maths',   1),
    ('place-value',         'Place value',                      'maths',   2),
    ('multiplication',      'Multiplication',                   'maths',   3),
    ('division',            'Division',                         'maths',   3),
    ('factors-multiples',   'Factors and multiples',            'maths',   4),
    ('fractions',           'Fractions',                        'maths',   4),
    ('decimals',            'Decimals',                         'maths',   5),
    ('percentages',         'Percentages',                      'maths',   6),
    ('ratio-proportion',    'Ratio and proportion',             'maths',   6),
    ('integers',            'Integers and negative numbers',    'maths',   6),
    ('exponents',           'Exponents and powers',             'maths',   7),
    ('algebraic-expressions','Algebraic expressions',           'maths',   7),
    ('linear-equations',    'Linear equations in one variable', 'maths',   7),
    ('algebra',             'Algebra',                          'maths',   7),
    ('polynomials',         'Polynomials',                      'maths',   9),
    ('quadratic-equations', 'Quadratic equations',              'maths',  10),
    ('coordinate-geometry', 'Coordinate geometry',              'maths',   9),
    ('trigonometry',        'Trigonometry',                     'maths',  10),
    ('functions',           'Functions',                        'maths',  11),
    ('limits',              'Limits',                           'maths',  11),
    ('derivatives',         'Differentiation',                  'maths',  11),

    ('matter',              'Matter and its states',            'science', 4),
    ('measurement',         'Measurement and units',            'science', 5),
    ('force',               'Force',                            'science', 6),
    ('motion',              'Motion, speed and velocity',       'science', 7),
    ('mass-weight',         'Mass and weight',                  'science', 7),
    ('acceleration',        'Acceleration',                     'science', 8),
    ('gravity',             'Gravity',                          'science', 8),
    ('newtons-laws',        'Newton''s laws of motion',         'science', 9),
    ('work-energy',         'Work, energy and power',           'science', 9),
    ('gravitation',         'Universal gravitation',            'science',11),

    ('cell',                'The cell',                         'biology', 6),
    ('cell-organelles',     'Cell organelles',                  'biology', 8),
    ('tissues',             'Tissues',                          'biology', 9),
    ('photosynthesis',      'Photosynthesis',                   'biology', 7),
    ('respiration',         'Respiration',                      'biology', 7),
    ('genetics',            'Heredity and genetics',            'biology',10)
ON CONFLICT (slug) DO NOTHING;

-- Edges: (concept) needs (prereq).
INSERT INTO concept_prereqs (concept_id, prereq_id)
SELECT c.id, p.id FROM concepts c, concepts p WHERE (c.slug, p.slug) IN (
    ('addition',             'counting'),
    ('subtraction',          'counting'),
    ('subtraction',          'addition'),
    ('place-value',          'counting'),
    ('multiplication',       'addition'),
    ('multiplication',       'place-value'),
    ('division',             'multiplication'),
    ('division',             'subtraction'),
    ('factors-multiples',    'multiplication'),
    ('factors-multiples',    'division'),
    ('fractions',            'division'),
    ('fractions',            'factors-multiples'),
    ('decimals',             'fractions'),
    ('decimals',             'place-value'),
    ('percentages',          'fractions'),
    ('percentages',          'decimals'),
    ('ratio-proportion',     'fractions'),
    ('integers',             'subtraction'),
    ('exponents',            'multiplication'),
    -- The spine behind the example this schema was built for: a Class 7 child
    -- stuck on algebra is usually missing fractions or integers, not algebra.
    ('algebraic-expressions','integers'),
    ('algebraic-expressions','exponents'),
    ('algebraic-expressions','fractions'),
    ('linear-equations',     'algebraic-expressions'),
    ('linear-equations',     'integers'),
    ('algebra',              'algebraic-expressions'),
    ('algebra',              'linear-equations'),
    ('polynomials',          'algebraic-expressions'),
    ('polynomials',          'exponents'),
    ('quadratic-equations',  'polynomials'),
    ('quadratic-equations',  'factors-multiples'),
    ('coordinate-geometry',  'linear-equations'),
    ('trigonometry',         'ratio-proportion'),
    ('trigonometry',         'coordinate-geometry'),
    ('functions',            'algebra'),
    ('functions',            'coordinate-geometry'),
    ('limits',               'functions'),
    ('derivatives',          'limits'),

    ('measurement',          'decimals'),
    ('force',                'measurement'),
    ('motion',               'measurement'),
    ('motion',               'ratio-proportion'),
    ('mass-weight',          'force'),
    ('acceleration',         'motion'),
    ('gravity',              'force'),
    ('gravity',              'mass-weight'),
    ('newtons-laws',         'acceleration'),
    ('newtons-laws',         'force'),
    ('work-energy',          'newtons-laws'),
    ('gravitation',          'gravity'),
    ('gravitation',          'newtons-laws'),
    ('gravitation',          'algebra'),

    ('cell-organelles',      'cell'),
    ('tissues',              'cell-organelles'),
    ('photosynthesis',       'cell'),
    ('respiration',          'cell'),
    ('genetics',             'cell-organelles')
) ON CONFLICT DO NOTHING;
