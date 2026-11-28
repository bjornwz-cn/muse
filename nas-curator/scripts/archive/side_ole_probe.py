import olefile, sys
for path in sys.argv[1:]:
    print("==", path)
    try:
        ole = olefile.OleFileIO(path)
        for entry in ole.listdir(streams=True, storages=True):
            s = "/".join(entry)
            try:
                sz = ole.get_size(entry)
            except Exception:
                sz = -1
            print("  stream:", s, sz)
    except Exception as e:
        print("  ERR", e)
