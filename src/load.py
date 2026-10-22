import duckdb, pathlib
con = duckdb.connect("data/belvo.duckdb")
for f in sorted(pathlib.Path("src/sql").glob("*.sql")):
    con.execute(f.read_text())
    print("ran", f.name)