"""
Client for the BoardGameGeek XML API v2 as at https://boardgamegeek.com/wiki/page/BGG_XML_API2
"""

from bs4 import BeautifulSoup
import requests
from time import sleep
import pprint
import html

BGG = 'https://www.boardgamegeek.com/xmlapi2/'


class Game:
    """
    Represents a single game or game expansion.
    """

    def __init__(self, item, threshold=0.5):
        """
        Parse a beautifulsoup object corresponding to the <item> tag for a game or expansion

        :param item:
            parser object from which information about the game / expansion can be extracted
        :param threshold:
            threshold used when extracting poll results, defaults to 0.5
        """
        # True if this is an expansion, false otherwise
        self.is_expansion = item['type'] == 'boardgameexpansion'
        # Thing ID used if we need to go back to BGG for any reason
        self.id = item['id']
        # Canonical name for this game or expansion
        self.name = item.find('name')['value']
        # Description
        self.description = html.unescape(item.find('description').text)
        # Year of publication
        self.year = item.find('yearpublished')['value']
        # min, max number of players from publisher
        self.players = item.find('minplayers')['value'], item.find('maxplayers')['value']
        # URL to an image of the box
        self.image_url = item.find('image').text
        # URL to a thumbnail of the box image
        self.thumbnail_url = item.find('thumbnail').text
        # Rating out of 10 (float)
        self.rating = float(item.find('statistics').find('ratings').find('average')['value'])
        # Complexity out of 5 (float)
        self.complexity = float(item.find('statistics').find('ratings').find('averageweight')['value'])
        # Publisher assessment of minimum player age
        self.minage = item.find('minage')['value']
        # Max, min playtime in minutes
        self.playtime = item.find('minplaytime')['value'], item.find('maxplaytime')['value']

        # Community recommended / best player counts
        def poll_num_players():
            for poll in item.find_all('poll'):
                if poll['name'] == 'suggested_numplayers' and poll.find_all('results'):
                    best_split = 0
                    best_value = None
                    recommended = []
                    for result in poll.find_all('results'):
                        num_players = result['numplayers']
                        votes = {sub_item['value']: int(sub_item['numvotes']) for sub_item in result.find_all('result')}
                        if votes:
                            best_votes = votes['Best']
                            recommended_votes = votes['Recommended']
                            no_votes = votes['Not Recommended']
                            total_votes = best_votes + recommended_votes + no_votes
                            if total_votes:
                                yes_votes = best_votes + recommended_votes
                                if (best_ratio := (best_votes / total_votes)) > best_split:
                                    best_split = best_ratio
                                    best_value = num_players
                                if yes_votes / total_votes > threshold:
                                    recommended.append(num_players)
                    if recommended:
                        return recommended[0], recommended[-1], best_value
            return None, None, None

        # Community rated player numbers, these are strings not ints as they can include values like '5+'
        self.community_players_min, self.community_players_max, self.community_players_best = poll_num_players()

        def poll_threshold(poll_name):
            """
            Look at an arbitrary poll, pick the first value in sequence which pushes the total vote count over
            the threshold value and return the string value of that item
            """
            for poll in item.find_all('poll'):
                if poll['name'] == poll_name and poll.find('results'):
                    results = poll.find('results').find_all('result')
                    total_votes = sum(int(result['numvotes']) for result in results)
                    if total_votes > 0:
                        cumulative = 0
                        for result in results:
                            cumulative += int(result['numvotes'])
                            if cumulative >= total_votes * threshold:
                                return result['value']

        # Community suggested player minimum age
        self.community_playerage = poll_threshold(poll_name='suggested_playerage')
        # Community voted language dependency
        self.community_language_dependence = poll_threshold(poll_name='language_dependence')

        def links(linktype):
            return list([link['value'] for link in item.find_all('link') if link['type'] == linktype])

        def inbound_links(linktype):
            return list([link['value'] for link in item.find_all('link') if
                         link['type'] == linktype and link.get('inbound') == 'true'])

        # None if this is not an expansion, or the canonical name of the thing it expands if so
        self.expands_name = inbound_links('boardgameexpansion')[0] if self.is_expansion else None
        # Empty list of expansions, this will be populated when instantiating a BoardGameCollection
        self.expansions = []
        # Categories, mechanics, artists etc etc as plain text collections
        self.categories = links('boardgamecategory')
        self.mechanics = links('boardgamemechanic')
        self.artists = links('boardgameartist')
        self.publishers = links('boardgamepublisher')
        self.designers = links('boardgamedesigner')
        self.compilations = links('boardgamecompilation')
        self.families = links('boardgamefamily')

    def good_for_players(self, player_count):
        """
        Returns True if this is a sensible number of players for this game, based on the community counts
        if available or the publisher ones if not.
        """
        min_players = int(self.community_players_min or self.players[0])
        max_players = self.community_players_max or self.players[1]

        if max_players.endswith('+'):
            exclusive = False
            max_players = int(max_players[:-1])
        else:
            exclusive = True
            max_players = int(max_players)

        if exclusive:
            return min_players <= player_count <= max_players
        else:
            return min_players <= player_count

    def __repr__(self):
        pp = pprint.PrettyPrinter(width=120, compact=False)
        return pp.pformat(self.__dict__)


class BoardGameCollection:
    """
    Represents a collection of owned games for a given user
    """

    def __init__(self, username=None):
        """
        Create a new collection, if username is specified then immediately attempt to retrieve games for that user
        """
        self._games = {}
        self._all_games = []
        if username:
            self.fetch(username=username)

    @property
    def games(self):
        """
        All games (does not include expansions, these are included as child properties of the base game objects)
        """
        return self._games.values()

    @property
    def games_by_id(self):
        """
        Games by ID, returns a dict of ID to Game for non-expansion game objects
        """
        return self._games

    @property
    def mechanics(self):
        """
        All the mechanics linked from any games or expansions in this collection
        """
        return set([item for sublist in [game.mechanics for game in self._all_games] for item in sublist])

    @property
    def categories(self):
        """
        All the categories linked from any games or expansions in this collection
        """
        return set([item for sublist in [game.categories for game in self._all_games] for item in sublist])

    def fetch(self, username):
        """
        Retrieve owned games for a specified user. Locates both games and expansions, and pushes expansions into the
        corresponding array for the base game object, assuming both are owned (if you don't own the base game but own
        an expansion you're probably doing it wrong)
        """
        self._games.clear()
        self._all_games.clear()
        # Get the collection representing games owned by this user
        collection_url = f'{BGG}/collection?username={username}&own=1'
        r = requests.get(collection_url)
        while r.status_code == 202:
            sleep(1)
            r = requests.get(collection_url)
        # Parse results to extract IDs
        soup = BeautifulSoup(r.text, 'lxml')
        ids = [item['objectid'] for item in soup.find_all('item')]
        # URL to get details for all these IDs in a single request
        things_url = f'{BGG}/thing?id={",".join(ids)}&stats=1'
        r = requests.get(things_url)
        soup = BeautifulSoup(r.text, 'lxml')
        games_by_name = {game.name: game for game in [Game(item) for item in soup.find_all('item')]}
        for name, game in games_by_name.items():
            self._all_games.append(game)
            if game.is_expansion and game.expands_name in games_by_name:
                games_by_name[game.expands_name].expansions.append(game)
            else:
                self._games[name] = game


collection = BoardGameCollection(username='mereden')
for g in sorted(collection.games, key=lambda g: g.rating, reverse=True):
    if g.good_for_players(2):
        print(
            f'{g.name} - rating={g.rating:.2f}, players : min={g.community_players_min}, '
            f'max={g.community_players_max}, best={g.community_players_best}')
        if g.expansions:
            for expansion in g.expansions:
                print(f'  * {expansion.name}')
