import os
for root, dirs, files in os.walk('D:\\AI_station\\推演agent'):
    for f in files:
        if f.endswith('.go'):
            full = os.path.join(root, f)
            rel = os.path.relpath(full, 'D:\\AI_station\\推演agent')
            print(rel)