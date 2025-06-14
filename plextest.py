from plexapi.server import PlexServer

plex = PlexServer("http://192.168.1.224:32400", "s7f2-x71kLuXF5xikzBd")

for section in plex.library.sections():
    for video in section.all():
        print(video.media[0].parts[0].file)
