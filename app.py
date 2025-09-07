import sqlite3
import requests
from datetime import datetime, timedelta
import json
from flask import Flask, render_template, request, redirect, url_for, flash
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Test mode - allows predictions on finished matches for testing
TEST_MODE = False  # Turn off test mode to use real API data

app = Flask(__name__)
app.secret_key = 'your-secret-key-change-this'  # Change this in production

class Database:
    def __init__(self, db_path=None):
        if db_path is None:
            # Use Render's persistent disk in production, local path in development
            db_path = os.environ.get('DATABASE_PATH', 'premier_league_predictions.db')
        self.db_path = db_path
        self.init_database()
    
    def get_connection(self):
        return sqlite3.connect(self.db_path)
    
    def init_database(self):
        """Initialize database tables"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Players table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(100) NOT NULL UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Game weeks table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS game_weeks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                week_number INTEGER NOT NULL UNIQUE,
                start_date DATE,
                end_date DATE,
                is_active BOOLEAN DEFAULT FALSE
            )
        ''')
        
        # Matches table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_match_id INTEGER UNIQUE,
                game_week INTEGER,
                home_team VARCHAR(100),
                away_team VARCHAR(100),
                match_date TIMESTAMP,
                home_score INTEGER DEFAULT NULL,
                away_score INTEGER DEFAULT NULL,
                result VARCHAR(10) DEFAULT NULL,  -- 'HOME', 'AWAY', 'DRAW'
                status VARCHAR(20) DEFAULT 'SCHEDULED',  -- 'SCHEDULED', 'LIVE', 'FINISHED'
                FOREIGN KEY (game_week) REFERENCES game_weeks(week_number)
            )
        ''')
        
        # Predictions table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id INTEGER,
                match_id INTEGER,
                prediction VARCHAR(10),  -- 'HOME', 'AWAY', 'DRAW'
                points_earned INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (player_id) REFERENCES players(id),
                FOREIGN KEY (match_id) REFERENCES matches(id),
                UNIQUE(player_id, match_id)
            )
        ''')
        
        conn.commit()
        conn.close()
    
    def get_all_players(self):
        """Get all players"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT id, name FROM players ORDER BY name')
        players = cursor.fetchall()
        conn.close()
        return players
    
    def get_matches_by_gameweek(self, game_week):
        """Get all matches for a specific game week"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, home_team, away_team, match_date, status, result, home_score, away_score
            FROM matches 
            WHERE game_week = ? 
            ORDER BY match_date
        ''', (game_week,))
        matches = cursor.fetchall()
        conn.close()
        return matches
    
    def get_weekly_results(self, game_week):
        """Get weekly results for all players in a specific gameweek"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Auto-calculate points first
        self.calculate_points_for_gameweek(game_week)
        
        # Get weekly points - only for THIS specific gameweek
        cursor.execute('''
            SELECT 
                pl.name as player_name,
                COALESCE(SUM(p.points_earned), 0) as weekly_points
            FROM players pl
            LEFT JOIN predictions p ON pl.id = p.player_id
            LEFT JOIN matches m ON p.match_id = m.id
            WHERE m.game_week = ? OR m.game_week IS NULL
            GROUP BY pl.id, pl.name
            ORDER BY weekly_points DESC, player_name
        ''', (game_week,))
        
        weekly_results = cursor.fetchall()
        
        # Get cumulative points - up to this gameweek
        cursor.execute('''
            SELECT 
                pl.name as player_name,
                COALESCE(SUM(p.points_earned), 0) as total_points
            FROM players pl
            LEFT JOIN predictions p ON pl.id = p.player_id
            LEFT JOIN matches m ON p.match_id = m.id
            WHERE m.game_week <= ? OR m.game_week IS NULL
            GROUP BY pl.id, pl.name
            ORDER BY total_points DESC, player_name
        ''', (game_week,))
        
        cumulative_results = cursor.fetchall()
        
        conn.close()
        return weekly_results, cumulative_results
    
    def get_overall_leaderboard(self):
        """Get overall leaderboard across all gameweeks"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT 
                pl.name as player_name,
                COALESCE(SUM(p.points_earned), 0) as total_points
            FROM players pl
            LEFT JOIN predictions p ON pl.id = p.player_id
            LEFT JOIN matches m ON p.match_id = m.id
            GROUP BY pl.id, pl.name
            ORDER BY total_points DESC, player_name
        ''', )
        
        results = cursor.fetchall()
        conn.close()
        return results
    
    def calculate_points_for_gameweek(self, game_week):
        """Calculate and update points for all predictions in a gameweek"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Get all predictions for finished matches in this gameweek
        cursor.execute('''
            SELECT p.id, p.prediction, m.result, p.player_id, m.home_team, m.away_team
            FROM predictions p
            JOIN matches m ON p.match_id = m.id
            WHERE m.game_week = ? AND m.result IS NOT NULL
        ''', (game_week,))
        
        predictions_to_update = cursor.fetchall()
        
        for pred_id, prediction, actual_result, player_id, home_team, away_team in predictions_to_update:
            points = 1 if prediction == actual_result else 0
            cursor.execute('''
                UPDATE predictions 
                SET points_earned = ? 
                WHERE id = ?
            ''', (points, pred_id))
        
        conn.commit()
        conn.close()
        
        return len(predictions_to_update)
    
    def add_default_players(self):
        """Add the 6 players to the database"""
        players = ["Biniam A", "Biniam G", "Biniam E", "Abel", "Siem", "Kubrom"]
        conn = self.get_connection()
        cursor = conn.cursor()
        
        for player in players:
            cursor.execute('INSERT OR IGNORE INTO players (name) VALUES (?)', (player,))
        
        conn.commit()
        conn.close()
        print("Real players added:", players)

class FootballAPI:
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "http://api.football-data.org/v4"
        self.headers = {"X-Auth-Token": api_key}
        self.premier_league_id = 2021
    
    def get_matches_by_matchday(self, matchday):
        """Get matches for a specific matchday (game week)"""
        url = f"{self.base_url}/competitions/{self.premier_league_id}/matches"
        params = {
            "matchday": matchday,
            "season": "2025"  # Current season 2025-26
        }
        
        try:
            response = requests.get(url, headers=self.headers, params=params)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"Error fetching matches for matchday {matchday}: {e}")
            return None
    
    def save_matches_to_db(self, matchday, db):
        """Fetch matches from API and save to database"""
        print(f"Fetching matchday {matchday} from API...")
        matches_data = self.get_matches_by_matchday(matchday)
        if not matches_data or 'matches' not in matches_data:
            print(f"No matches found for matchday {matchday}")
            return False
        
        conn = db.get_connection()
        cursor = conn.cursor()
        
        matches_saved = 0
        for match in matches_data['matches']:
            # Parse match date
            match_date = datetime.fromisoformat(match['utcDate'].replace('Z', '+00:00'))
            
            # Determine status - In TEST_MODE, allow predictions on finished matches
            status = match['status']
            if not TEST_MODE:  # Normal mode - respect actual status
                if status == 'FINISHED':
                    status = 'FINISHED'
                elif status in ['IN_PLAY', 'PAUSED']:
                    status = 'LIVE'
                else:
                    status = 'SCHEDULED'
            else:  # TEST_MODE - allow predictions on everything
                status = 'SCHEDULED'
            
            # Determine result
            result = None
            home_score = None
            away_score = None
            
            if match['score']['fullTime']['home'] is not None:
                home_score = match['score']['fullTime']['home']
                away_score = match['score']['fullTime']['away']
                
                if home_score > away_score:
                    result = 'HOME'
                elif away_score > home_score:
                    result = 'AWAY'
                else:
                    result = 'DRAW'
            
            cursor.execute('''
                INSERT OR REPLACE INTO matches 
                (api_match_id, game_week, home_team, away_team, match_date, 
                 home_score, away_score, result, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                match['id'], matchday, 
                match['homeTeam']['name'], match['awayTeam']['name'],
                match_date, home_score, away_score, result, status
            ))
            matches_saved += 1
        
        conn.commit()
        conn.close()
        print(f"Saved {matches_saved} matches for matchday {matchday}")
        return True

# Initialize database and API
db = Database()
db.add_default_players()

# Get API key with manual parsing fallback
API_KEY = os.getenv("FOOTBALL_API_KEY")
if not API_KEY:
    try:
        with open('.env', 'r') as f:
            for line in f:
                line = line.strip()
                if line.startswith('FOOTBALL_API_KEY='):
                    API_KEY = line.split('=', 1)[1]
                    break
    except:
        pass

api = FootballAPI(API_KEY) if API_KEY else None

# Flask Routes
@app.route('/')
def home():
    """Home page with player and game week selection"""
    players = db.get_all_players()
    game_weeks = list(range(1, 39))
    return render_template('home.html', players=players, game_weeks=game_weeks)

@app.route('/predictions/<int:player_id>/<int:game_week>')
def predictions(player_id, game_week):
    """Predictions page for specific player and game week"""
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT name FROM players WHERE id = ?', (player_id,))
    player = cursor.fetchone()
    
    if not player:
        flash('Player not found')
        return redirect(url_for('home'))
    
    # Load matches for this game week (from API if not exists)
    matches = db.get_matches_by_gameweek(game_week)
    if not matches and api:
        if api.save_matches_to_db(game_week, db):
            matches = db.get_matches_by_gameweek(game_week)
    
    # Get existing predictions for this player and game week
    cursor.execute('''
        SELECT m.id, COALESCE(p.prediction, '') as prediction
        FROM matches m
        LEFT JOIN predictions p ON m.id = p.match_id AND p.player_id = ?
        WHERE m.game_week = ?
    ''', (player_id, game_week))
    predictions_result = cursor.fetchall()
    predictions_data = {row[0]: row[1] for row in predictions_result}
    
    conn.close()
    
    return render_template('predictions.html', 
                         player=player, 
                         player_id=player_id,
                         game_week=game_week, 
                         matches=matches,
                         predictions=predictions_data)

@app.route('/submit_predictions', methods=['POST'])
def submit_predictions():
    """Handle prediction submissions"""
    player_id = int(request.form.get('player_id'))
    game_week = int(request.form.get('game_week'))
    
    conn = db.get_connection()
    cursor = conn.cursor()
    
    predictions_count = 0
    for key, value in request.form.items():
        if key.startswith('prediction_'):
            match_id = key.replace('prediction_', '')
            
            cursor.execute('''
                INSERT OR REPLACE INTO predictions 
                (player_id, match_id, prediction, updated_at)
                VALUES (?, ?, ?, ?)
            ''', (player_id, match_id, value, datetime.now()))
            predictions_count += 1
    
    conn.commit()
    conn.close()
    
    flash(f'Predictions submitted successfully! ({predictions_count} predictions saved)')
    return redirect(url_for('prediction_summary', player_id=player_id, game_week=game_week))

@app.route('/summary/<int:player_id>/<int:game_week>')
def prediction_summary(player_id, game_week):
    """Show what the player predicted"""
    conn = db.get_connection()
    cursor = conn.cursor()
    
    cursor.execute('SELECT name FROM players WHERE id = ?', (player_id,))
    player = cursor.fetchone()
    
    if not player:
        flash('Player not found')
        return redirect(url_for('home'))
    
    cursor.execute('''
        SELECT m.home_team, m.away_team, m.match_date, p.prediction
        FROM matches m
        JOIN predictions p ON m.id = p.match_id
        WHERE p.player_id = ? AND m.game_week = ?
        ORDER BY m.match_date
    ''', (player_id, game_week))
    
    predictions = cursor.fetchall()
    conn.close()
    
    return render_template('summary.html', 
                         player=player, 
                         player_id=player_id,
                         game_week=game_week, 
                         predictions=predictions)

@app.route('/results')
def results_home():
    """Results home page with options"""
    return render_template('results_home.html')

@app.route('/results/weekly/<int:game_week>')
def weekly_results(game_week):
    """Weekly results for a specific gameweek"""
    matches = db.get_matches_by_gameweek(game_week)
    if not matches and api:
        api.save_matches_to_db(game_week, db)
    
    weekly_results, cumulative_results = db.get_weekly_results(game_week)
    
    return render_template('weekly_results.html', 
                         game_week=game_week, 
                         weekly_results=weekly_results,
                         cumulative_results=cumulative_results)

@app.route('/results/leaderboard')
def leaderboard():
    """Overall leaderboard"""
    results = db.get_overall_leaderboard()
    return render_template('leaderboard.html', results=results)


    

if __name__ == '__main__':
    # Use PORT environment variable for production
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)